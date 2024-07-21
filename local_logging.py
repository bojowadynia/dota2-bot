import logging
import logging.config
import yaml


with open("logging.yaml", "rt") as f:
    logging_config = yaml.safe_load(f.read())
logging.config.dictConfig(logging_config)
