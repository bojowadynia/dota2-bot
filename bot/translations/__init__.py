import json
import pathlib

TRANSLATIONS = {}
# Language in which commands will be shown
LANG = "PL"

# import translations
path = pathlib.Path(__file__).absolute().parent / "PL.json"
with open(path, encoding="utf-8") as polish_file:
    polish = json.load(polish_file)
    TRANSLATIONS["PL"] = polish


def t(translation_string: str):
    generic_message = "Translation for this system string is not available, call the admin!"
    return TRANSLATIONS[LANG].get(translation_string, generic_message)
