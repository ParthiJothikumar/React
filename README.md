cd C:\Users\808222\Azure_Foundry_Project\orchestrator-api
python -c "import time; t=time.time(); import app.main; print('imports:', round(time.time()-t,2), 's')"
python -c "import time; from app.deps import foundry_client as f; t=time.time(); f.warm(); print('warm:', round(time.time()-t,2), 's')"
