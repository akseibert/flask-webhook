        if "Supervisor" in sess["structured_data"].get("people", []):
            sess["structured_data"]["people"] = [p for p in sess["structured_data"].get("people", []) if p != "Supervisor"]
            sess["structured_data"]["roles"] = [r for r in sess["structured_data"].get("roles", []) if r.get("name") != "Supervisor"]
            log_event("cleaned_supervisor_entries", chat_id=chat_id)

        if "voice" in msg:
            text = transcribe_voice(msg["voice"]["file_id"])
            if not text:
                send_message(chat_id, "⚠️ Couldn't understand the audio. Please speak clearly (e.g., 'add site Downtown Project').")
                return "ok", 200
            log_event("transcribed_voice", text=text)

        if sess.get("awaiting_reset_confirmation", False):
            normalized_text = re.sub(r'[.!?]\s*$', '', text.strip()).lower()
            log_event("reset_confirmation", text=normalized_text, pending_input=sess["pending_input"])
            if normalized_text in ("yes", "new", "new report"):
                sess["structured_data"] = blank_report()
                sess["awaiting_correction"] = False
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["command_history"].clear()
                save_session(session_data)
                tpl = summarize_report(sess["structured_data"])
                send_message(chat_id, f"**Starting a fresh report**\n\n{tpl}\n\nSpeak or type your first field (e.g., 'add site Downtown Project').")
                return "ok", 200
            elif normalized_text in ("no", "existing", "continue"):
                text = sess["pending_input"]
                sess["awaiting_reset_confirmation"] = False
                sess["pending_input"] = None
                sess["last_interaction"] = time()
            else:
                send_message(chat_id, "Please clarify: Reset the report? Reply 'yes' or 'no'.")
                return "ok", 200

        if sess.get("awaiting_spelling_correction"):
            field, old_value = sess["awaiting_spelling_correction"]
            new_value = text.strip()
            log_event("spelling_correction_response", field=field, old_value=old_value, new_value=new_value)
            if new_value.lower() == old_value.lower():
                sess["awaiting_spelling_correction"] = None
                save_session(session_data)
                send_message(chat_id, f"⚠️ New value '{new_value}' is the same as the old value '{old_value}'. Please provide a different spelling for '{old_value}' in {field}.")
                return "ok", 200
            sess["awaiting_spelling_correction"] = None
            sess["command_history"].append(sess["structured_data"].copy())
            if field in ["company", "roles", "tools", "service", "issues"]:
                data_field = (
                    "name" if field == "company" else
                    "description" if field == "issues" else
                    "item" if field == "tools" else
                    "task" if field == "service" else
                    "name" if field == "roles" else None
                )
                sess["structured_data"][field] = [
                    {data_field: new_value if string_similarity(item.get(data_field, ""), old_value) > 0.7 else item[data_field],
                     **({} if field != "roles" else {"role": item["role"]})}
                    for item in sess["structured_data"].get(field, [])
                    if isinstance(item, dict)
                ]
                if field == "roles" and new_value not in sess["structured_data"].get("people", []):
                    sess["structured_data"]["people"].append(new_value)
            elif field in ["people"]:
                sess["structured_data"]["people"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("people", [])]
                sess["structured_data"]["roles"] = [
                    {"name": new_value, "role": role["role"]} if string_similarity(role.get("name", ""), old_value) > 0.7 else role
                    for role in sess["structured_data"].get("roles", [])
                ]
            elif field in ["activities"]:
                sess["structured_data"]["activities"] = [new_value if string_similarity(item, old_value) > 0.7 else item for item in sess["structured_data"].get("activities", [])]
            else:
                sess["structured_data"][field] = new_value
            save_session(session_data)
            tpl = summarize_report(sess["structured_data"])
            send_message(chat_id, f"Corrected {field} from '{old_value}' to '{new_value}'.\n\nUpdated report:\n\n{tpl}\n\nAnything else to add or correct?")
            return "ok", 200

        return handle_command(chat_id, text, sess)
    except Exception as e:
        log_event("webhook_error", error=str(e))
        return "error", 500

