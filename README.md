python -u -c "print('go'); import time; t=time.time(); import app.db.database; print('database mod', round(time.time()-t,2))"
python -u -c "print('go'); import time; t=time.time(); import mssql_python; print('mssql driver', round(time.time()-t,2))"
python -u -c "print('go'); import time; t=time.time(); import app.deps; print('deps', round(time.time()-t,2))"
python -u -c "print('go'); import time; t=time.time(); import app.main; print('app.main', round(time.time()-t,2))"
