import os
os.environ['DATABASE_URL']='sqlite:///./data/test.db'
os.environ['LANGSMITH_TRACING']='false'
os.environ['EMAIL_MODE']='sandbox'
os.environ['LLM_PROVIDER']='openai'
if os.getenv('RUN_LIVE_OPENAI') != '1':
    os.environ['OPENAI_API_KEY']=''
os.environ['SHOPPING_LIVE_DISCOVERY']='false'
os.environ['CATALOG_AUTO_REFRESH']='false'
os.environ['SMTP_HOST']='localhost'
os.environ['SMTP_PORT']='1025'
import pytest
from backend.database import Base,engine,init_db
from sandbox.seed import seed
@pytest.fixture(autouse=True)
def clean_db():
    from backend import models
    Base.metadata.drop_all(engine); init_db(); seed()
