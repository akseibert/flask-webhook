==> Exited with status 1
==> Common ways to troubleshoot your deploy: https://render.com/docs/troubleshooting-deploys
==> Running 'gunicorn app:app'
Traceback (most recent call last):
  File "/opt/render/project/src/.venv/bin/gunicorn", line 8, in <module>
    sys.exit(run())
             ^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/wsgiapp.py", line 67, in run
    WSGIApplication("%(prog)s [OPTIONS] [APP_MODULE]").run()
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/base.py", line 236, in run
    super().run()
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/base.py", line 72, in run
    Arbiter(self).run()
    ^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/arbiter.py", line 58, in __init__
    self.setup(app)
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/arbiter.py", line 118, in setup
    self.app.wsgi()
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/base.py", line 67, in wsgi
    self.callable = self.load()
                    ^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/wsgiapp.py", line 58, in load
    return self.load_wsgiapp()
           ^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/app/wsgiapp.py", line 48, in load_wsgiapp
    return util.import_app(self.app_uri)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/opt/render/project/src/.venv/lib/python3.11/site-packages/gunicorn/util.py", line 371, in import_app
    mod = importlib.import_module(module)
          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/local/lib/python3.11/importlib/__init__.py", line 126, in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "<frozen importlib._bootstrap>", line 1204, in _gcd_import
  File "<frozen importlib._bootstrap>", line 1176, in _find_and_load
  File "<frozen importlib._bootstrap>", line 1147, in _find_and_load_unlocked
  File "<frozen importlib._bootstrap>", line 690, in _load_unlocked
  File "<frozen importlib._bootstrap_external>", line 936, in exec_module
  File "<frozen importlib._bootstrap_external>", line 1074, in get_code
  File "<frozen importlib._bootstrap_external>", line 1004, in source_to_code
  File "<frozen importlib._bootstrap>", line 241, in _call_with_frames_removed
  File "/opt/render/project/src/app.py", line 59
    gpt_prompt = """
                 ^
SyntaxError: unterminated triple-quoted string literal (detected at line 86)
