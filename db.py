import os
from sqlalchemy.ext.automap import automap_base
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session

user = os.getenv("DB_USER")
password = os.getenv("DB_PW")
host = os.getenv("DB_HOST")
port = int(os.getenv("DB_PORT"))
dbname = os.getenv("DB_NAME")

Base = automap_base()

engine = create_engine(f"mysql+mysqlconnector://{user}:{password}@{host}:{port}/{dbname}", echo=True)

Base.prepare(autoload_with=engine)

LadderQueue = Base.classes.ladder_ladderqueue
LadderSettings = Base.classes.ladder_laddersettings

if __name__ == "__main__":
    session = Session(engine)
