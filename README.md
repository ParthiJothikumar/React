$env:SQLITE_DB_PATH = "local.db"
python -u -c "print('go'); import time; t=time.time(); import app.deps; print('deps', round(time.time()-t,2))"
