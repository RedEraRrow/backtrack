"""Timezone data, world map rendering, and timezone_select widget."""
from __future__ import annotations
import re
import sys
import math

# Allow running this file directly (`python3 src/utils/tz_widget.py`) for
# standalone testing/fun — put the repo root on sys.path before the package
# imports below, which otherwise require running as `python3 -m src.utils...`.
if __name__ == '__main__' and __package__ in (None, ''):
    import os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.utils.prompt_core import (
    _Widget, _read_key, _wait_for_keypress,
    _set_raw, _restore_term_attrs, _get_term_attrs,
    _hint, _hint_lines, _visible_rows, _cols, _rows, C,
)
from src.utils import ui_utils
from src import state as _state
from src.state import QuitToTerminal

_TIMEZONES = [
    # (utc_h, utc_m, lat, lon, tz_name, abbr, city, is_capital)
    # UTC-12
    (-12,  0,   0.2, -176.5, "Etc/GMT+12",                     "IDLW",      "International Date Line West", False),
    # UTC-11
    (-11,  0, -14.3, -170.7, "Pacific/Pago_Pago",              "SST",       "Pago Pago",                    True),
    (-11,  0, -13.3, -176.2, "Pacific/Niue",                   "NUT",       "Alofi",                        True),
    # UTC-10
    (-10,  0,  21.3, -157.8, "Pacific/Honolulu",               "HST",       "Honolulu",                     False),
    (-10,  0,  20.9, -156.5, "Pacific/Honolulu",               "HST",       "Maui",                         False),
    (-10,  0, -17.5, -149.6, "Pacific/Tahiti",                 "TAHT",      "Papeete",                      True),
    (-10,  0, -21.2, -159.8, "Pacific/Rarotonga",              "CKT",       "Avarua",                       True),
    # UTC-9:30
    ( -9, 30, -9.4,  -139.0, "Pacific/Marquesas",              "MART",      "Nuku Hiva",                    False),
    # UTC-9
    ( -9,  0,  61.2, -149.9, "America/Anchorage",              "AKST/AKDT", "Anchorage",                    False),
    ( -9,  0,  64.8, -147.7, "America/Anchorage",              "AKST/AKDT", "Fairbanks",                    False),
    ( -9,  0,  57.0, -135.3, "America/Sitka",                  "AKST/AKDT", "Sitka",                        False),
    # UTC-8
    ( -8,  0,  34.0, -118.2, "America/Los_Angeles",            "PST/PDT",   "Los Angeles",                  False),
    ( -8,  0,  37.8, -122.4, "America/Los_Angeles",            "PST/PDT",   "San Francisco",                False),
    ( -8,  0,  47.6, -122.3, "America/Los_Angeles",            "PST/PDT",   "Seattle",                      False),
    ( -8,  0,  45.5, -122.7, "America/Los_Angeles",            "PST/PDT",   "Portland",                     False),
    ( -8,  0,  49.2, -123.1, "America/Vancouver",              "PST/PDT",   "Vancouver",                    False),
    ( -8,  0,  32.7, -117.2, "America/Los_Angeles",            "PST/PDT",   "San Diego",                    False),
    ( -8,  0,  36.7, -119.8, "America/Los_Angeles",            "PST/PDT",   "Fresno",                       False),
    ( -8,  0,  38.6, -121.5, "America/Los_Angeles",            "PST/PDT",   "Sacramento",                   False),
    ( -8,  0,  51.1, -114.1, "America/Edmonton",               "MST/MDT",   "Calgary",                      False),
    # UTC-7
    ( -7,  0,  39.7, -104.9, "America/Denver",                 "MST/MDT",   "Denver",                       False),
    ( -7,  0,  40.8, -111.9, "America/Denver",                 "MST/MDT",   "Salt Lake City",               False),
    ( -7,  0,  35.1, -106.7, "America/Denver",                 "MST/MDT",   "Albuquerque",                  False),
    ( -7,  0,  32.3, -110.9, "America/Phoenix",                "MST",       "Tucson",                       False),
    ( -7,  0,  33.4, -112.1, "America/Phoenix",                "MST",       "Phoenix",                      False),
    ( -7,  0,  51.0, -114.1, "America/Edmonton",               "MST/MDT",   "Edmonton",                     False),
    ( -7,  0,  19.0, -103.3, "America/Mazatlan",               "MST/MDT",   "Guadalajara",                  False),
    ( -7,  0,  23.2, -106.4, "America/Mazatlan",               "MST/MDT",   "Mazatlán",                     False),
    ( -7,  0,  29.1, -110.9, "America/Hermosillo",             "MST",       "Hermosillo",                   False),
    # UTC-6
    ( -6,  0,  41.8,  -87.6, "America/Chicago",                "CST/CDT",   "Chicago",                      False),
    ( -6,  0,  29.8,  -95.4, "America/Chicago",                "CST/CDT",   "Houston",                      False),
    ( -6,  0,  32.8,  -96.8, "America/Chicago",                "CST/CDT",   "Dallas",                       False),
    ( -6,  0,  44.9,  -93.1, "America/Chicago",                "CST/CDT",   "Minneapolis",                  False),
    ( -6,  0,  38.6,  -90.2, "America/Chicago",                "CST/CDT",   "St. Louis",                    False),
    ( -6,  0,  29.9,  -90.1, "America/Chicago",                "CST/CDT",   "New Orleans",                  False),
    ( -6,  0,  53.5,  -113.5, "America/Edmonton",              "MST/MDT",   "Winnipeg",                     False),
    ( -6,  0,  19.4,  -99.1, "America/Mexico_City",            "CST/CDT",   "Mexico City",                  True),
    ( -6,  0,  20.9,  -89.6, "America/Merida",                 "CST/CDT",   "Mérida",                       False),
    ( -6,  0,  25.7,  -100.3,"America/Monterrey",              "CST/CDT",   "Monterrey",                    False),
    ( -6,  0,  22.2,  -97.8, "America/Monterrey",              "CST/CDT",   "Tampico",                      False),
    ( -6,  0,  52.1, -106.7, "America/Regina",                 "CST",       "Regina",                       False),
    ( -6,  0,  13.7,  -89.2, "America/El_Salvador",            "CST",       "San Salvador",                 True),
    ( -6,  0,  14.6,  -90.5, "America/Guatemala",              "CST",       "Guatemala City",               True),
    ( -6,  0,  14.1,  -87.2, "America/Tegucigalpa",            "CST",       "Tegucigalpa",                  True),
    ( -6,  0,  12.1,  -86.3, "America/Managua",                "CST",       "Managua",                      True),
    ( -6,  0,   9.9,  -84.1, "America/Costa_Rica",             "CST",       "San José",                     True),
    ( -6,  0,  17.3,  -89.9, "America/Belize",                 "CST",       "Belmopan",                     True),
    # UTC-5
    ( -5,  0,  40.7,  -74.0, "America/New_York",               "EST/EDT",   "New York",                     False),
    ( -5,  0,  42.4,  -71.1, "America/New_York",               "EST/EDT",   "Boston",                       False),
    ( -5,  0,  25.8,  -80.2, "America/New_York",               "EST/EDT",   "Miami",                        False),
    ( -5,  0,  39.9,  -75.2, "America/New_York",               "EST/EDT",   "Philadelphia",                 False),
    ( -5,  0,  38.9,  -77.0, "America/New_York",               "EST/EDT",   "Washington DC",                True),
    ( -5,  0,  43.7,  -79.4, "America/Toronto",                "EST/EDT",   "Toronto",                      False),
    ( -5,  0,  45.4,  -75.7, "America/Toronto",                "EST/EDT",   "Ottawa",                       False),
    ( -5,  0,  45.5,  -73.6, "America/Toronto",                "EST/EDT",   "Montreal",                     False),
    ( -5,  0,  30.3,  -81.7, "America/New_York",               "EST/EDT",   "Jacksonville",                 False),
    ( -5,  0,  36.2,  -86.8, "America/Chicago",                "CST/CDT",   "Nashville",                    False),
    ( -5,  0,   4.7,  -74.1, "America/Bogota",                 "COT",       "Bogotá",                       True),
    ( -5,  0,   6.3,  -75.6, "America/Bogota",                 "COT",       "Medellín",                     False),
    ( -5,  0,   3.4,  -76.5, "America/Bogota",                 "COT",       "Cali",                         False),
    ( -5,  0, -12.0,  -77.0, "America/Lima",                   "PET",       "Lima",                         True),
    ( -5,  0,  -8.1,  -79.0, "America/Lima",                   "PET",       "Trujillo",                     False),
    ( -5,  0,   9.0,  -79.5, "America/Panama",                 "EST",       "Panama City",                  True),
    ( -5,  0,  -0.2,  -78.5, "America/Guayaquil",              "ECT",       "Quito",                        True),
    ( -5,  0,  -2.2,  -79.9, "America/Guayaquil",              "ECT",       "Guayaquil",                    False),
    # UTC-4:30
    ( -4, 30,  10.5,  -66.9, "America/Caracas",                "VET",       "Caracas",                      True),
    # UTC-4
    ( -4,  0,  44.6,  -63.6, "America/Halifax",                "AST/ADT",   "Halifax",                      False),
    ( -4,  0,  17.1,  -61.8, "America/Antigua",                "AST",       "St. John's (Antigua)",         True),
    ( -4,  0,  13.1,  -59.6, "America/Barbados",               "AST",       "Bridgetown",                   True),
    ( -4,  0,  10.7,  -61.5, "America/Port_of_Spain",          "AST",       "Port of Spain",                True),
    ( -4,  0,  18.0,  -76.8, "America/Jamaica",                "EST",       "Kingston",                     True),
    ( -4,  0,  18.5,  -69.9, "America/Santo_Domingo",          "AST",       "Santo Domingo",                True),
    ( -4,  0,  18.5,  -72.3, "America/Port-au-Prince",         "EST/EDT",   "Port-au-Prince",               True),
    ( -4,  0,  32.3,  -64.8, "Atlantic/Bermuda",               "AST/ADT",   "Hamilton",                     True),
    ( -4,  0, -33.5,  -70.7, "America/Santiago",               "CLT/CLST",  "Santiago",                     True),
    ( -4,  0, -23.0,  -43.2, "America/Sao_Paulo",              "BRT/BRST",  "Rio de Janeiro",               False),
    ( -4,  0, -16.5,  -68.1, "America/La_Paz",                 "BOT",       "La Paz",                       True),
    ( -4,  0,  -3.1,  -60.0, "America/Manaus",                 "AMT",       "Manaus",                       False),
    ( -4,  0,  -1.5,  -48.5, "America/Belem",                  "BRT",       "Belém",                        False),
    ( -4,  0,  -2.5,  -44.3, "America/Fortaleza",              "BRT",       "São Luís",                     False),
    ( -4,  0,  11.8,  -15.6, "Africa/Bissau",                  "GMT",       "Bissau",                       True),
    # UTC-3:30
    ( -3, 30,  47.6,  -52.7, "America/St_Johns",               "NST/NDT",   "St. John's (NL)",              False),
    # UTC-3
    ( -3,  0, -34.9,  -56.2, "America/Montevideo",             "UYT",       "Montevideo",                   True),
    ( -3,  0, -34.6,  -58.4, "America/Argentina/Buenos_Aires", "ART",       "Buenos Aires",                 True),
    ( -3,  0, -31.4,  -64.2, "America/Argentina/Cordoba",      "ART",       "Córdoba",                      False),
    ( -3,  0, -33.0,  -71.6, "America/Santiago",               "CLT/CLST",  "Valparaíso",                   False),
    ( -3,  0, -15.8,  -47.9, "America/Sao_Paulo",              "BRT/BRST",  "Brasília",                     True),
    ( -3,  0, -23.5,  -46.6, "America/Sao_Paulo",              "BRT/BRST",  "São Paulo",                    False),
    ( -3,  0,  -3.7,  -38.5, "America/Fortaleza",              "BRT",       "Fortaleza",                    False),
    ( -3,  0, -12.9,  -38.4, "America/Bahia",                  "BRT",       "Salvador",                     False),
    ( -3,  0, -8.0,   -34.9, "America/Recife",                 "BRT",       "Recife",                       False),
    ( -3,  0,   5.8,  -55.2, "America/Paramaribo",             "SRT",       "Paramaribo",                   True),
    ( -3,  0,   6.8,  -58.2, "America/Guyana",                 "GYT",       "Georgetown",                   True),
    ( -3,  0,   4.9,  -52.3, "America/Cayenne",                "GFT",       "Cayenne",                      False),
    # UTC-2
    ( -2,  0, -54.3,  -36.5, "Atlantic/South_Georgia",         "GST",       "South Georgia",                False),
    ( -2,  0,  -3.8,  -32.4, "America/Noronha",                "FNT",       "Fernando de Noronha",          False),
    # UTC-1
    ( -1,  0,  37.7,  -25.7, "Atlantic/Azores",                "AZOT/AZOST","Ponta Delgada",                False),
    ( -1,  0,  14.9,  -23.5, "Atlantic/Cape_Verde",            "CVT",       "Praia",                        True),
    # UTC+0
    (  0,  0,  51.5,   -0.1, "Europe/London",                  "GMT/BST",   "London",                       True),
    (  0,  0,  53.3,   -6.3, "Europe/Dublin",                  "GMT/IST",   "Dublin",                       True),
    (  0,  0,  51.5,   -3.2, "Europe/London",                  "GMT/BST",   "Cardiff",                      False),
    (  0,  0,  55.9,   -3.2, "Europe/London",                  "GMT/BST",   "Edinburgh",                    False),
    (  0,  0,  53.8,   -1.5, "Europe/London",                  "GMT/BST",   "Leeds",                        False),
    (  0,  0,  53.5,   -2.2, "Europe/London",                  "GMT/BST",   "Manchester",                   False),
    (  0,  0,  52.5,   -1.9, "Europe/London",                  "GMT/BST",   "Birmingham",                   False),
    (  0,  0,  64.1,  -21.9, "Atlantic/Reykjavik",             "GMT",       "Reykjavik",                    True),
    (  0,  0,   5.6,   -0.2, "Africa/Accra",                   "GMT",       "Accra",                        True),
    (  0,  0,  33.6,   -7.6, "Africa/Casablanca",              "WET/WEST",  "Casablanca",                   False),
    (  0,  0,  34.0,   -6.8, "Africa/Casablanca",              "WET/WEST",  "Rabat",                        True),
    (  0,  0,   6.4,    2.3, "Africa/Porto-Novo",              "WAT",       "Porto-Novo",                   True),
    (  0,  0,   6.4,    2.4, "Africa/Lagos",                   "WAT",       "Cotonou",                      False),
    (  0,  0,  13.5,    2.1, "Africa/Niamey",                  "WAT",       "Niamey",                       True),
    (  0,  0,  12.4,   -1.5, "Africa/Ouagadougou",             "GMT",       "Ouagadougou",                  True),
    (  0,  0,  12.7,   -8.0, "Africa/Bamako",                  "GMT",       "Bamako",                       True),
    (  0,  0,  15.6,  -32.4, "Africa/Dakar",                   "GMT",       "Dakar",                        True),
    (  0,  0,  11.9,  -15.6, "Africa/Bissau",                  "GMT",       "Conakry",                      True),
    (  0,  0,   8.5,  -13.2, "Africa/Freetown",                "GMT",       "Freetown",                     True),
    (  0,  0,   6.3,  -10.8, "Africa/Monrovia",                "GMT",       "Monrovia",                     True),
    (  0,  0,   5.3,   -4.0, "Africa/Abidjan",                 "GMT",       "Yamoussoukro",                 True),
    (  0,  0,   5.4,   -3.9, "Africa/Abidjan",                 "GMT",       "Abidjan",                      False),
    (  0,  0,  38.7,   -9.1, "Europe/Lisbon",                  "WET/WEST",  "Lisbon",                       True),
    (  0,  0,  41.2,   -8.6, "Europe/Lisbon",                  "WET/WEST",  "Porto",                        False),
    # UTC+1
    (  1,  0,  48.9,    2.3, "Europe/Paris",                   "CET/CEST",  "Paris",                        True),
    (  1,  0,  43.3,    5.4, "Europe/Paris",                   "CET/CEST",  "Marseille",                    False),
    (  1,  0,  45.7,    4.8, "Europe/Paris",                   "CET/CEST",  "Lyon",                         False),
    (  1,  0,  43.6,    1.4, "Europe/Paris",                   "CET/CEST",  "Toulouse",                     False),
    (  1,  0,  52.5,   13.4, "Europe/Berlin",                  "CET/CEST",  "Berlin",                       True),
    (  1,  0,  53.6,   10.0, "Europe/Berlin",                  "CET/CEST",  "Hamburg",                      False),
    (  1,  0,  48.1,   11.6, "Europe/Berlin",                  "CET/CEST",  "Munich",                       False),
    (  1,  0,  50.9,    6.9, "Europe/Berlin",                  "CET/CEST",  "Cologne",                      False),
    (  1,  0,  50.1,    8.7, "Europe/Berlin",                  "CET/CEST",  "Frankfurt",                    False),
    (  1,  0,  53.1,    8.8, "Europe/Berlin",                  "CET/CEST",  "Bremen",                       False),
    (  1,  0,  51.5,    7.5, "Europe/Berlin",                  "CET/CEST",  "Dortmund",                     False),
    (  1,  0,  41.9,   12.5, "Europe/Rome",                    "CET/CEST",  "Rome",                         True),
    (  1,  0,  45.5,    9.2, "Europe/Rome",                    "CET/CEST",  "Milan",                        False),
    (  1,  0,  40.8,   14.3, "Europe/Rome",                    "CET/CEST",  "Naples",                       False),
    (  1,  0,  45.4,   12.3, "Europe/Rome",                    "CET/CEST",  "Venice",                       False),
    (  1,  0,  43.8,   11.2, "Europe/Rome",                    "CET/CEST",  "Florence",                     False),
    (  1,  0,  40.4,   -3.7, "Europe/Madrid",                  "CET/CEST",  "Madrid",                       True),
    (  1,  0,  41.4,    2.2, "Europe/Madrid",                  "CET/CEST",  "Barcelona",                    False),
    (  1,  0,  39.5,   -0.4, "Europe/Madrid",                  "CET/CEST",  "Valencia",                     False),
    (  1,  0,  37.4,   -5.9, "Europe/Madrid",                  "CET/CEST",  "Seville",                      False),
    (  1,  0,  36.7,   -4.4, "Europe/Madrid",                  "CET/CEST",  "Málaga",                       False),
    (  1,  0,  47.4,    8.5, "Europe/Zurich",                  "CET/CEST",  "Zurich",                       False),
    (  1,  0,  46.9,    7.5, "Europe/Zurich",                  "CET/CEST",  "Bern",                         True),
    (  1,  0,  46.2,    6.1, "Europe/Zurich",                  "CET/CEST",  "Geneva",                       False),
    (  1,  0,  50.8,    4.4, "Europe/Brussels",                "CET/CEST",  "Brussels",                     True),
    (  1,  0,  51.2,    4.4, "Europe/Brussels",                "CET/CEST",  "Antwerp",                      False),
    (  1,  0,  52.4,    4.9, "Europe/Amsterdam",               "CET/CEST",  "Amsterdam",                    True),
    (  1,  0,  51.9,    4.5, "Europe/Amsterdam",               "CET/CEST",  "Rotterdam",                    False),
    (  1,  0,  52.1,    5.1, "Europe/Amsterdam",               "CET/CEST",  "Utrecht",                      False),
    (  1,  0,  59.9,   10.7, "Europe/Oslo",                    "CET/CEST",  "Oslo",                         True),
    (  1,  0,  60.4,    5.3, "Europe/Oslo",                    "CET/CEST",  "Bergen",                       False),
    (  1,  0,  57.7,   12.0, "Europe/Stockholm",               "CET/CEST",  "Gothenburg",                   False),
    (  1,  0,  59.3,   18.1, "Europe/Stockholm",               "CET/CEST",  "Stockholm",                    True),
    (  1,  0,  55.7,   12.6, "Europe/Copenhagen",              "CET/CEST",  "Copenhagen",                   True),
    (  1,  0,  56.2,   10.2, "Europe/Copenhagen",              "CET/CEST",  "Aarhus",                       False),
    (  1,  0,  52.2,   21.0, "Europe/Warsaw",                  "CET/CEST",  "Warsaw",                       True),
    (  1,  0,  50.1,   19.9, "Europe/Warsaw",                  "CET/CEST",  "Kraków",                       False),
    (  1,  0,  50.0,   14.4, "Europe/Prague",                  "CET/CEST",  "Prague",                       True),
    (  1,  0,  47.5,   19.0, "Europe/Budapest",                "CET/CEST",  "Budapest",                     True),
    (  1,  0,  48.1,   17.1, "Europe/Bratislava",              "CET/CEST",  "Bratislava",                   True),
    (  1,  0,  46.1,   14.5, "Europe/Ljubljana",               "CET/CEST",  "Ljubljana",                    True),
    (  1,  0,  45.8,   16.0, "Europe/Zagreb",                  "CET/CEST",  "Zagreb",                       True),
    (  1,  0,  43.8,   18.4, "Europe/Sarajevo",                "CET/CEST",  "Sarajevo",                     True),
    (  1,  0,  42.4,   19.3, "Europe/Podgorica",               "CET/CEST",  "Podgorica",                    True),
    (  1,  0,  42.0,   21.4, "Europe/Skopje",                  "CET/CEST",  "Skopje",                       True),
    (  1,  0,  41.3,   19.8, "Europe/Tirane",                  "CET/CEST",  "Tirana",                       True),
    (  1,  0,   6.5,    3.4, "Africa/Lagos",                   "WAT",       "Lagos",                        False),
    (  1,  0,   9.1,    7.5, "Africa/Lagos",                   "WAT",       "Abuja",                        True),
    (  1,  0,   3.9,    11.5,"Africa/Douala",                  "WAT",       "Yaoundé",                      True),
    (  1,  0,   4.4,    9.7, "Africa/Douala",                  "WAT",       "Douala",                       False),
    (  1,  0,   4.4,   18.6, "Africa/Bangui",                  "WAT",       "Bangui",                       True),
    (  1,  0,  12.1,   15.1, "Africa/Ndjamena",                "WAT",       "N'Djamena",                    True),
    (  1,  0,   3.9,    11.5,"Africa/Libreville",              "WAT",       "Libreville",                   True),
    (  1,  0,   0.4,    9.5, "Africa/Malabo",                  "WAT",       "Malabo",                       True),
    (  1,  0,   0.4,    9.5, "Africa/Libreville",              "WAT",       "São Tomé",                     True),
    (  1,  0,  36.8,   10.2, "Africa/Tunis",                   "CET",       "Tunis",                        True),
    (  1,  0,  32.9,   13.2, "Africa/Tripoli",                 "EET",       "Tripoli",                      True),
    # UTC+2
    (  2,  0,  37.9,   23.7, "Europe/Athens",                  "EET/EEST",  "Athens",                       True),
    (  2,  0,  40.6,   22.9, "Europe/Athens",                  "EET/EEST",  "Thessaloniki",                 False),
    (  2,  0,  30.0,   31.2, "Africa/Cairo",                   "EET",       "Cairo",                        True),
    (  2,  0,  31.2,   29.9, "Africa/Cairo",                   "EET",       "Alexandria",                   False),
    (  2,  0, -26.2,   28.0, "Africa/Johannesburg",            "SAST",      "Johannesburg",                 False),
    (  2,  0, -33.9,   18.4, "Africa/Johannesburg",            "SAST",      "Cape Town",                    False),
    (  2,  0, -29.9,   30.9, "Africa/Johannesburg",            "SAST",      "Durban",                       False),
    (  2,  0, -25.7,   28.2, "Africa/Johannesburg",            "SAST",      "Pretoria",                     True),
    (  2,  0, -25.9,   32.6, "Africa/Maputo",                  "CAT",       "Maputo",                       True),
    (  2,  0, -18.9,   32.7, "Africa/Harare",                  "CAT",       "Harare",                       True),
    (  2,  0, -15.4,   28.3, "Africa/Lusaka",                  "CAT",       "Lusaka",                       True),
    (  2,  0, -24.7,   25.9, "Africa/Gaborone",                "CAT",       "Gaborone",                     True),
    (  2,  0, -22.6,   17.1, "Africa/Windhoek",                "WAT/WAST",  "Windhoek",                     True),
    (  2,  0, -29.4,   27.5, "Africa/Maseru",                  "SAST",      "Maseru",                       True),
    (  2,  0, -26.3,   31.1, "Africa/Mbabane",                 "SAST",      "Mbabane",                      True),
    (  2,  0,  50.5,   30.5, "Europe/Kiev",                    "EET/EEST",  "Kyiv",                         True),
    (  2,  0,  49.8,   24.0, "Europe/Kiev",                    "EET/EEST",  "Lviv",                         False),
    (  2,  0,  46.5,   30.7, "Europe/Kiev",                    "EET/EEST",  "Odessa",                       False),
    (  2,  0,  56.9,   24.1, "Europe/Riga",                    "EET/EEST",  "Riga",                         True),
    (  2,  0,  54.7,   25.3, "Europe/Vilnius",                 "EET/EEST",  "Vilnius",                      True),
    (  2,  0,  59.4,   24.7, "Europe/Tallinn",                 "EET/EEST",  "Tallinn",                      True),
    (  2,  0,  60.2,   25.0, "Europe/Helsinki",                "EET/EEST",  "Helsinki",                     True),
    (  2,  0,  60.4,   25.7, "Europe/Helsinki",                "EET/EEST",  "Tampere",                      False),
    (  2,  0,  44.4,   26.1, "Europe/Bucharest",               "EET/EEST",  "Bucharest",                    True),
    (  2,  0,  44.2,   28.6, "Europe/Bucharest",               "EET/EEST",  "Constanța",                    False),
    (  2,  0,  42.7,   23.3, "Europe/Sofia",                   "EET/EEST",  "Sofia",                        True),
    (  2,  0,  42.1,   24.7, "Europe/Sofia",                   "EET/EEST",  "Plovdiv",                      False),
    (  2,  0,  44.8,   20.5, "Europe/Belgrade",                "CET/CEST",  "Belgrade",                     True),
    (  2,  0,  43.9,   17.7, "Europe/Sarajevo",                "CET/CEST",  "Mostar",                       False),
    (  2,  0,  42.7,   21.2, "Europe/Pristina",                "CET/CEST",  "Pristina",                     True),
    (  2,  0,  31.8,   35.2, "Asia/Jerusalem",                 "IST/IDT",   "Jerusalem",                    True),
    (  2,  0,  32.1,   34.8, "Asia/Jerusalem",                 "IST/IDT",   "Tel Aviv",                     False),
    (  2,  0,  33.9,   35.5, "Asia/Beirut",                    "EET/EEST",  "Beirut",                       True),
    (  2,  0,  33.5,   36.3, "Asia/Damascus",                  "EET/EEST",  "Damascus",                     True),
    (  2,  0,  32.0,   36.0, "Asia/Amman",                     "EET/EEST",  "Amman",                        True),
    (  2,  0,  15.6,   32.5, "Africa/Khartoum",                "EAT",       "Khartoum",                     True),
    (  2,  0,   4.9,   31.6, "Africa/Juba",                    "EAT",       "Juba",                         True),
    (  2,  0,   2.0,   45.3, "Africa/Mogadishu",               "EAT",       "Mogadishu",                    True),
    # UTC+3
    (  3,  0,  55.7,   37.6, "Europe/Moscow",                  "MSK",       "Moscow",                       True),
    (  3,  0,  59.9,   30.3, "Europe/Moscow",                  "MSK",       "St. Petersburg",               False),
    (  3,  0,  56.8,   60.6, "Asia/Yekaterinburg",             "YEKT",      "Yekaterinburg",                False),
    (  3,  0,  53.9,   27.6, "Europe/Minsk",                   "FET",       "Minsk",                        True),
    (  3,  0,  24.7,   46.7, "Asia/Riyadh",                    "AST",       "Riyadh",                       True),
    (  3,  0,  21.5,   39.2, "Asia/Riyadh",                    "AST",       "Jeddah",                       False),
    (  3,  0,  24.5,   54.4, "Asia/Dubai",                     "GST",       "Abu Dhabi",                    True),
    (  3,  0,  25.3,   51.5, "Asia/Qatar",                     "AST",       "Doha",                         True),
    (  3,  0,  26.2,   50.6, "Asia/Bahrain",                   "AST",       "Manama",                       True),
    (  3,  0,  23.6,   58.6, "Asia/Muscat",                    "GST",       "Muscat",                       True),
    (  3,  0,  15.3,   38.9, "Africa/Asmara",                  "EAT",       "Asmara",                       True),
    (  3,  0,  11.6,   43.1, "Africa/Djibouti",                "EAT",       "Djibouti",                     True),
    (  3,  0,  -1.3,   36.8, "Africa/Nairobi",                 "EAT",       "Nairobi",                      True),
    (  3,  0,  -4.0,   39.7, "Africa/Nairobi",                 "EAT",       "Mombasa",                      False),
    (  3,  0,  41.0,   28.9, "Europe/Istanbul",                "TRT",       "Istanbul",                     False),
    (  3,  0,  39.9,   32.9, "Europe/Istanbul",                "TRT",       "Ankara",                       True),
    (  3,  0,  38.4,   27.1, "Europe/Istanbul",                "TRT",       "Izmir",                        False),
    (  3,  0,   9.0,   38.7, "Africa/Addis_Ababa",             "EAT",       "Addis Ababa",                  True),
    (  3,  0, -6.2,    35.7, "Africa/Dar_es_Salaam",           "EAT",       "Dodoma",                       True),
    (  3,  0, -6.8,    39.3, "Africa/Dar_es_Salaam",           "EAT",       "Dar es Salaam",                False),
    (  3,  0, -13.0,   27.8, "Africa/Lusaka",                  "CAT",       "Lilongwe",                     True),
    (  3,  0,  -4.3,   15.3, "Africa/Kinshasa",                "WAT",       "Kinshasa",                     True),
    (  3,  0,  -1.7,   29.2, "Africa/Kigali",                  "CAT",       "Kigali",                       True),
    (  3,  0,  -3.4,   29.4, "Africa/Bujumbura",               "CAT",       "Bujumbura",                    True),
    (  3,  0,   0.3,   32.6, "Africa/Kampala",                 "EAT",       "Kampala",                      True),
    # UTC+3:30
    (  3, 30,  35.7,   51.4, "Asia/Tehran",                    "IRST/IRDT", "Tehran",                       True),
    (  3, 30,  32.7,   51.7, "Asia/Tehran",                    "IRST/IRDT", "Isfahan",                      False),
    (  3, 30,  36.3,   59.6, "Asia/Tehran",                    "IRST/IRDT", "Mashhad",                      False),
    # UTC+4
    (  4,  0,  25.2,   55.3, "Asia/Dubai",                     "GST",       "Dubai",                        False),
    (  4,  0,  25.3,   55.5, "Asia/Dubai",                     "GST",       "Sharjah",                      False),
    (  4,  0,  40.4,   49.8, "Asia/Baku",                      "AZT",       "Baku",                         True),
    (  4,  0,  41.7,   44.8, "Asia/Tbilisi",                   "GET",       "Tbilisi",                      True),
    (  4,  0,  40.2,   44.5, "Asia/Yerevan",                   "AMT",       "Yerevan",                      True),
    (  4,  0,  37.9,   58.4, "Asia/Ashgabat",                  "TMT",       "Ashgabat",                     True),
    (  4,  0,  57.8,   40.9, "Europe/Moscow",                  "MSK",       "Samara",                       False),
    (  4,  0, -20.2,   57.5, "Indian/Mauritius",               "MUT",       "Port Louis",                   True),
    (  4,  0,  -4.6,   55.5, "Indian/Mahe",                    "SCT",       "Victoria",                     True),
    # UTC+4:30
    (  4, 30,  34.5,   69.2, "Asia/Kabul",                     "AFT",       "Kabul",                        True),
    (  4, 30,  31.6,   65.7, "Asia/Kabul",                     "AFT",       "Kandahar",                     False),
    # UTC+5
    (  5,  0,  24.9,   67.0, "Asia/Karachi",                   "PKT",       "Karachi",                      False),
    (  5,  0,  33.7,   73.1, "Asia/Karachi",                   "PKT",       "Islamabad",                    True),
    (  5,  0,  31.5,   74.3, "Asia/Karachi",                   "PKT",       "Lahore",                       False),
    (  5,  0,  34.0,   71.6, "Asia/Karachi",                   "PKT",       "Peshawar",                     False),
    (  5,  0,  41.3,   69.3, "Asia/Tashkent",                  "UZT",       "Tashkent",                     True),
    (  5,  0,  39.7,   66.9, "Asia/Samarkand",                 "UZT",       "Samarkand",                    False),
    (  5,  0,  37.9,   58.4, "Asia/Ashgabat",                  "TMT",       "Mary",                         False),
    (  5,  0,  42.9,   74.6, "Asia/Bishkek",                   "KGT",       "Bishkek",                      True),
    (  5,  0,  43.3,   76.9, "Asia/Almaty",                    "ALMT",      "Almaty",                       False),
    (  5,  0,  51.2,   71.4, "Asia/Almaty",                    "ALMT",      "Astana",                       True),
    (  5,  0,  56.8,   60.6, "Asia/Yekaterinburg",             "YEKT",      "Ekaterinburg",                 False),
    (  5,  0,  55.0,   73.4, "Asia/Omsk",                      "OMST",      "Omsk",                         False),
    # UTC+5:30
    (  5, 30,  19.1,   72.9, "Asia/Kolkata",                   "IST",       "Mumbai",                       False),
    (  5, 30,  28.6,   77.2, "Asia/Kolkata",                   "IST",       "Delhi",                        True),
    (  5, 30,  22.6,   88.4, "Asia/Kolkata",                   "IST",       "Kolkata",                      False),
    (  5, 30,  12.9,   77.6, "Asia/Kolkata",                   "IST",       "Bangalore",                    False),
    (  5, 30,  13.1,   80.3, "Asia/Kolkata",                   "IST",       "Chennai",                      False),
    (  5, 30,  17.4,   78.5, "Asia/Kolkata",                   "IST",       "Hyderabad",                    False),
    (  5, 30,  23.0,   72.6, "Asia/Kolkata",                   "IST",       "Ahmedabad",                    False),
    (  5, 30,  18.5,   73.9, "Asia/Kolkata",                   "IST",       "Pune",                         False),
    (  5, 30,   6.9,   79.9, "Asia/Colombo",                   "IST",       "Colombo",                      False),
    (  5, 30,   7.0,   80.0, "Asia/Colombo",                   "IST",       "Sri Jayawardenepura Kotte",     True),
    # UTC+5:45
    (  5, 45,  27.7,   85.3, "Asia/Kathmandu",                 "NPT",       "Kathmandu",                    True),
    # UTC+6
    (  6,  0,  23.7,   90.4, "Asia/Dhaka",                     "BST",       "Dhaka",                        True),
    (  6,  0,  22.3,   91.8, "Asia/Dhaka",                     "BST",       "Chittagong",                   False),
    (  6,  0,  27.5,   90.4, "Asia/Thimphu",                   "BTT",       "Thimphu",                      True),
    (  6,  0,  55.0,   73.4, "Asia/Omsk",                      "OMST",      "Novosibirsk",                  False),
    (  6,  0,  53.2,   50.2, "Europe/Samara",                  "SAMT",      "Ufa",                          False),
    # UTC+6:30
    (  6, 30,  16.8,   96.2, "Asia/Rangoon",                   "MMT",       "Yangon",                       False),
    (  6, 30,  21.9,   96.1, "Asia/Rangoon",                   "MMT",       "Naypyidaw",                    True),
    # UTC+7
    (  7,  0,  13.7,  100.5, "Asia/Bangkok",                   "ICT",       "Bangkok",                      True),
    (  7,  0,  18.8,  102.6, "Asia/Vientiane",                 "ICT",       "Vientiane",                    True),
    (  7,  0,  11.6,  104.9, "Asia/Phnom_Penh",                "ICT",       "Phnom Penh",                   True),
    (  7,  0,  21.0,  105.8, "Asia/Ho_Chi_Minh",               "ICT",       "Hanoi",                        True),
    (  7,  0,  10.8,  106.7, "Asia/Ho_Chi_Minh",               "ICT",       "Ho Chi Minh City",             False),
    (  7,  0,  16.1,  108.2, "Asia/Ho_Chi_Minh",               "ICT",       "Da Nang",                      False),
    (  7,  0,  -6.2,  106.8, "Asia/Jakarta",                   "WIB",       "Jakarta",                      True),
    (  7,  0,  -7.2,  112.7, "Asia/Jakarta",                   "WIB",       "Surabaya",                     False),
    (  7,  0,  -6.9,  107.6, "Asia/Jakarta",                   "WIB",       "Bandung",                      False),
    (  7,  0,  56.0,   92.8, "Asia/Krasnoyarsk",               "KRAT",      "Krasnoyarsk",                  False),
    (  7,  0,  54.8,   56.0, "Asia/Yekaterinburg",             "YEKT",      "Chelyabinsk",                  False),
    # UTC+8
    (  8,  0,  39.9,  116.4, "Asia/Shanghai",                  "CST",       "Beijing",                      True),
    (  8,  0,  31.2,  121.5, "Asia/Shanghai",                  "CST",       "Shanghai",                     False),
    (  8,  0,  23.1,  113.3, "Asia/Shanghai",                  "CST",       "Guangzhou",                    False),
    (  8,  0,  22.3,  114.2, "Asia/Hong_Kong",                 "HKT",       "Hong Kong",                    False),
    (  8,  0,  22.2,  113.5, "Asia/Macau",                     "CST",       "Macau",                        False),
    (  8,  0,  22.6,  120.3, "Asia/Taipei",                    "CST",       "Kaohsiung",                    False),
    (  8,  0,  25.0,  121.5, "Asia/Taipei",                    "CST",       "Taipei",                       True),
    (  8,  0,  30.6,  104.1, "Asia/Shanghai",                  "CST",       "Chengdu",                      False),
    (  8,  0,  43.8,  87.6,  "Asia/Urumqi",                    "CST",       "Urumqi",                       False),
    (  8,  0,  36.1,  103.8, "Asia/Shanghai",                  "CST",       "Lanzhou",                      False),
    (  8,  0,  34.3,  108.9, "Asia/Shanghai",                  "CST",       "Xi'an",                        False),
    (  8,  0,  29.6,  106.6, "Asia/Shanghai",                  "CST",       "Chongqing",                    False),
    (  8,  0,   3.1,  101.7, "Asia/Kuala_Lumpur",              "MYT",       "Kuala Lumpur",                 True),
    (  8,  0,   5.4,  100.3, "Asia/Kuala_Lumpur",              "MYT",       "Penang",                       False),
    (  8,  0,   1.3,  103.8, "Asia/Singapore",                 "SGT",       "Singapore",                    True),
    (  8,  0,  14.6,  121.0, "Asia/Manila",                    "PHT",       "Manila",                       True),
    (  8,  0,  10.3,  123.9, "Asia/Manila",                    "PHT",       "Cebu",                         False),
    (  8,  0,  -8.6,  115.2, "Asia/Makassar",                  "WITA",      "Denpasar",                     False),
    (  8,  0, -5.1,   119.4, "Asia/Makassar",                  "WITA",      "Makassar",                     False),
    (  8,  0, -31.9,  115.9, "Australia/Perth",                "AWST",      "Perth",                        False),
    (  8,  0,  47.9,  106.9, "Asia/Ulaanbaatar",               "ULAT",      "Ulaanbaatar",                  True),
    (  8,  0,  51.7,  36.2,  "Asia/Irkutsk",                   "IRKT",      "Irkutsk",                      False),
    (  8,  0,  1.5,   110.3, "Asia/Kuching",                   "MYT",       "Kuching",                      False),
    (  8,  0,  5.8,   116.1, "Asia/Kuching",                   "MYT",       "Kota Kinabalu",                False),
    (  8,  0,  4.9,   115.0, "Asia/Brunei",                    "BNT",       "Bandar Seri Begawan",          True),
    (  8,  0, -8.5,   125.6, "Asia/Dili",                      "TLT",       "Dili",                         True),
    # UTC+9
    (  9,  0,  35.7,  139.7, "Asia/Tokyo",                     "JST",       "Tokyo",                        True),
    (  9,  0,  34.7,  135.5, "Asia/Tokyo",                     "JST",       "Osaka",                        False),
    (  9,  0,  35.2,  136.9, "Asia/Tokyo",                     "JST",       "Nagoya",                       False),
    (  9,  0,  43.1,  141.3, "Asia/Tokyo",                     "JST",       "Sapporo",                      False),
    (  9,  0,  33.6,  130.4, "Asia/Tokyo",                     "JST",       "Fukuoka",                      False),
    (  9,  0,  37.6,  127.0, "Asia/Seoul",                     "KST",       "Seoul",                        True),
    (  9,  0,  35.1,  129.0, "Asia/Seoul",                     "KST",       "Busan",                        False),
    (  9,  0,  39.0,  125.8, "Asia/Pyongyang",                 "KST",       "Pyongyang",                    True),
    (  9,  0,  62.0,  129.7, "Asia/Yakutsk",                   "YAKT",      "Yakutsk",                      False),
    (  9,  0, -8.6,   125.6, "Asia/Dili",                      "TLT",       "Kupang",                       False),
    (  9,  0, -0.9,   134.1, "Asia/Jayapura",                  "WIT",       "Jayapura",                     False),
    (  9,  0,  22.3,   114.2,"Asia/Hong_Kong",                 "HKT",       "Palau",                        True),
    # UTC+9:30
    (  9, 30, -34.9,  138.6, "Australia/Adelaide",             "ACST/ACDT", "Adelaide",                     False),
    (  9, 30, -12.4,  130.8, "Australia/Darwin",               "ACST",      "Darwin",                       False),
    # UTC+10
    ( 10,  0, -33.9,  151.2, "Australia/Sydney",               "AEST/AEDT", "Sydney",                       False),
    ( 10,  0, -37.8,  145.0, "Australia/Melbourne",            "AEST/AEDT", "Melbourne",                    False),
    ( 10,  0, -27.5,  153.0, "Australia/Brisbane",             "AEST",      "Brisbane",                     False),
    ( 10,  0, -35.3,  149.1, "Australia/Sydney",               "AEST/AEDT", "Canberra",                     True),
    ( 10,  0,  43.1,  131.9, "Asia/Vladivostok",               "VLAT",      "Vladivostok",                  False),
    ( 10,  0,  13.5,  144.8, "Pacific/Guam",                   "ChST",      "Hagåtña",                      True),
    ( 10,  0,   7.4,  134.5, "Pacific/Palau",                  "PWT",       "Ngerulmud",                    True),
    ( 10,  0,   6.9,  158.2, "Pacific/Pohnpei",                "PONT",      "Palikir",                      True),
    # UTC+10:30
    ( 10, 30, -31.5,  159.1, "Australia/Lord_Howe",            "LHST/LHDT", "Lord Howe Island",             False),
    # UTC+11
    ( 11,  0,  -9.4,  160.0, "Pacific/Guadalcanal",            "SBT",       "Honiara",                      True),
    ( 11,  0, -17.7,  168.3, "Pacific/Efate",                  "VUT",       "Port Vila",                    True),
    ( 11,  0, -22.3,  166.5, "Pacific/Noumea",                 "NCT",       "Nouméa",                       False),
    ( 11,  0,  59.6,  150.8, "Asia/Magadan",                   "MAGT",      "Magadan",                      False),
    ( 11,  0,  47.0,  142.7, "Asia/Sakhalin",                  "SAKT",      "Yuzhno-Sakhalinsk",            False),
    # UTC+12
    ( 12,  0, -36.9,  174.8, "Pacific/Auckland",               "NZST/NZDT", "Auckland",                     False),
    ( 12,  0, -41.3,  174.8, "Pacific/Auckland",               "NZST/NZDT", "Wellington",                   True),
    ( 12,  0, -43.5,  172.6, "Pacific/Auckland",               "NZST/NZDT", "Christchurch",                 False),
    ( 12,  0, -18.1,  178.4, "Pacific/Fiji",                   "FJT",       "Suva",                         True),
    ( 12,  0,  53.0,  158.7, "Asia/Kamchatka",                 "PETT",      "Petropavlovsk-Kamchatsky",     False),
    ( 12,  0,  64.7,  177.5, "Asia/Anadyr",                    "ANAT",      "Anadyr",                       False),
    ( 12,  0, -13.9,  -171.9,"Pacific/Apia",                   "WST",       "Funafuti",                     True),
    ( 12,  0,   7.1,  171.4, "Pacific/Majuro",                 "MHT",       "Majuro",                       True),
    ( 12,  0,   7.3,  168.7, "Pacific/Kwajalein",              "MHT",       "Kwajalein",                    False),
    # UTC+12:45
    ( 12, 45, -43.9, -176.6, "Pacific/Chatham",                "CHAST/CHADT","Waitangi",                    False),
    # UTC+13
    ( 13,  0, -21.1, -175.2, "Pacific/Tongatapu",              "TOT",       "Nuku'alofa",                   True),
    ( 13,  0, -13.8, -172.0, "Pacific/Apia",                   "WST",       "Apia",                         True),
    ( 13,  0,  -8.5,  179.2, "Pacific/Fakaofo",                "TKT",       "Fakaofo",                      False),
    # UTC+14
    ( 14,  0,   1.9,  157.5, "Pacific/Kiritimati",             "LINT",      "Kiritimati",                   False),
]


# Pre-computed 72×36 land/sea bitmap (5°/pixel, zlib+base64).
# Each bit = 1 land, 0 sea. Row-major, top = 90°N, left = 180°W.
_WORLD_MAP_DATA = (
    "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"
)
# 2880×1440 bitmap, 0.125°/pixel (Natural Earth 110m coastlines).
# Column c → lon = −180 + (c+0.5)×(360/2880), row r → lat = 90 − (r+0.5)×(180/1440).

_WORLD_BITMAP: list[list[int]] | None = None


def _get_world_bitmap() -> list[list[int]]:
    """Decode and cache the 2880x1440 world coastline bitmap (built lazily, once)."""
    global _WORLD_BITMAP
    if _WORLD_BITMAP is None:
        import zlib, base64 as _b64
        raw = zlib.decompress(_b64.b64decode(_WORLD_MAP_DATA))
        bm = []
        bit = 0
        for _ in range(1440):
            row = []
            for _ in range(2880):
                row.append((raw[bit >> 3] >> (7 - (bit & 7))) & 1)
                bit += 1
            bm.append(row)
        _WORLD_BITMAP = bm
    return _WORLD_BITMAP


_WORLD_TZ_DATA = (
    "eNrt3T1ro0mzxnEHE4iDgwkcOBCDWBQ4cKBAgQIxmEF4hXEgWDGYgzkomGC+/yc4frdkSbfufqnu"
    "qur/xbO7nvGs9ST7o6iurj4768q3hAzic56S7wm5iM9lQoYJGSVknJKrhFzHZ5KSaUpm8Zmn5CY+"
    "i5Qs43OfklV81il5iM9jQjadOcNnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfG7S5w0+4zM+4zM+4zM+4zM+4zM+"
    "4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+4zM+Cwef"
    "8Rmf8Rmfdfq8bzU+4zM+43MDPqc4Pa4TfMZnfDbs86Apn4cS0ezzR5rxeXZTJ/iMz/gc6/NQPMp9"
    "jjEan/EZn/HZhc+fRI9VhvoZn/EZnxv1eWwo+IzP+IzPbfg8dhN8zpJl0eAzPuOze5sPKY3P+IzP"
    "+GzU57HT4LMan1d6g8/4rNdn7zTjs6TPKwfBZ3xW6fO4CZvr+hwD+o2W4DM+43MFn8ft0IzP+IzP"
    "+Fzq+iA4Rwzb4TP9Z3zGZ9GFG3l8HjWHMz7jMz7jswmfNV8MlLulgs/4fCwPtYLP+Ny5W8MYw5NJ"
    "9B1CfMZnVTbjMz6fWHtkrkw+rOc1PuOzPZzxWZ/PlzmSbyWdC577EY3P+KzKZnzG55PrQn347K9+"
    "5v6gf5zxuYjPF2WbG8E+61/lnOrzNT7jszmb8bmAzyVfs4ryWf1bKKk+698vis+KfH5QFXyW87n4"
    "a4MxPo/wGZ/xWaPN+Czpc4XXYGP7z36ATjgexOdmfH6wE3zO7vNFUqqcD7oBmvNBuz63XCbjs7jP"
    "F3lS3ueRoxbHJKHBgc81fW67jYHPsj5fZIu2+TpbQCeMb+BzPZ+bbzPjs6DPFzlT636KD6Dx2aDP"
    "HAPis5jPF5lT7/5gt9HMb+CziM+MaQjx3LzPFxKp6XOn0PiMz9l9ZoxOTOfmfb5w6HMX0ZwP4nNe"
    "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"
    "PwWfe/GMz9p8znp9EJ+5P4jPxnw+O8NntT6P8Rmf8blhn8/wWa/PmXnGZ3zGZ1M+n+Gz1foZn/EZ"
    "n337fIbPRn1mvg6f8dm3z2dn+Gy0v8H8Mz7js2+fz/DZqM/cT8FnfHbu8xk+6/Y5t874jM/4bMXn"
    "M3zW7fMYn/EZn9v0+ewMn3X7PMZnfMbnNn0+w2d8xmd8xmeNPp/hs/r3UwR4xmd8xmf9Pp/hs9X+"
    "8ygp+IzP+Kzc57MzfDY6vzEa4TM+47Nnn8/w2c77sPiMz/jcks9n+Kzb5yE+4zM+4zM+m6ufx/iM"
    "z/iMz/iMz/iMz/iMz/gcsMEOn/EZn/EZn5X4/NVWSZ+vpILP+IzP+OzC52EnsPiMz/iMz/hczeeh"
    "FM/4jM/4jM/4LFQ/pzY48Bmf8Rmf8Tmpfh7iMz7jMz7jMz7jMz7jMz7jc3+fjwktvF8Un/EZn/EZ"
    "n4dxQKfO2Ln2eZo7+IzP+IzP/YFOvaaCz/iMz/iMz+k+HyI69SIhPuMzPuMzPmfxeU/o1JX9+IzP"
    "+Czuc4re+GzJ5yE+4zM+4zM+6/S5qxHNfiR8xmd8xueqPndv5MBn5uvwGZ/xuaLPuYzGZ3zGZ3zG"
    "5/w+50Aan/EZn/EZn4V8TjQan/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZ"
    "n/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZ"
    "n/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZ"
    "n/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/EZn/E5l8/jMT7jMz7jMz7jMz7jMz7j"
    "Mz7jMz7jMz7jM/1nfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMbn6j6/oIrP+IzP+IzP+IzP+IzP+IzP"
    "vX2mv4HP+IzP+Ez9jM/4jM/4jM/4jM/4jM/4zPwGPuMzPuMzPuMzPuMzPuMzPuMzPuMzPuMzPuMz"
    "PuMzPuMzPuMzPuMzPuMzPuNzos+r9OAzPuMzPuNzdp9XK3zGZ3zGZ3xW6PMKn/HZm89bWuIzPlM/"
    "R5q8fsBnfM7t846W+IzP+FyyZsZnfO7NMz7jMz7jMz5r8XmMz/i8JfEHxszX4TM+4zM+q/I5e/AZ"
    "n/GZ/gY+4zP3U/DZ53wdPuNzFp+fYMRnfMbnzPPP+IzPGXy+x2d8xmd8xmeNPt/jMz7jM/cH8Vmh"
    "z28w4jM+4zM+4zM+4zM+4zM+4zPzG/iMz/iMz9wfxGd8xmd8TvL55Z/vvw5AG5/xGZ/xGZ/FfE4q"
    "qvEZn/EZn/EZn/EZn/EZn635/PTfPj7jMz7jMz5r8vnpP/qXv+EzPuMzPuOzOp/fgs/4jM/4jM/4"
    "jM/4jM/4jM/4jM/4jM/43LLPO97iMz7jMz7jM/cH8Rmf8Rmf8Rmf8Rmf8Rmf8Rmf8Rmf8Rmf8Rmf"
    "8Rmf8Rmf8Rmf8Rmf8Rmf8dmnz/vQ4jM+4zM+4zM+4zM+4zM+V/P5q4/4jM/4jM/4jM9xPtN/xmd8"
    "xmd8xmd8xmd8xmd8pr+Bz/iMz/hs2Gfm6/AZn/EZn9X5zPwzPuMzPuMzPuMzPif5PJngMz631d/A"
    "Z3xW7/MHj/iMz/iMz/isxucdHvEZn5u6P0h/A5/xGZ/xmfoZn/EZn/EZn134/A2f8Rmf8Rmfuxse"
    "1M/4LO3zca/xGZ+b3l936Jf4jM/lfA6ft8NnfG5nv+hhg/EZn0v4HDMPjc8t+3zqD/vzeRMffMbn"
    "eJ8j76vgMz6343NS8BmfY32Ovk+Iz/iMz/iMz4I+J9z3xmd8xmd8xmd8xmd8xmd8xmd8xmd8xmd8"
    "xmd8xmd8xmd8xmd8xmd8xmd8xmfup+AzPuMz76fgMz7L+RwQfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZn"
    "fMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfMZnfJb2+f8BrZeVTg=="
)
# 1440×720 timezone raster. Byte = round((utc_offset+12)*4); 255=ocean.
# Derived from Natural Earth ne_10m_time_zones.

_WORLD_TZMAP: bytearray | None = None


def _get_tzmap() -> bytearray:
    """Decode and cache the 1440x720 timezone raster (built lazily, once)."""
    global _WORLD_TZMAP
    if _WORLD_TZMAP is None:
        import zlib, base64 as _b64
        _WORLD_TZMAP = bytearray(zlib.decompress(_b64.b64decode(_WORLD_TZ_DATA)))
    return _WORLD_TZMAP

# Braille dot bit indices per sub-pixel position within a 2-col × 4-row cell.
# dc=0 (left column), dc=1 (right column); dr=0..3 (top-to-bottom rows).
_BRAILLE_BIT = [
    [0, 1, 2, 6],   # left column: rows 0-3 → braille bits 0,1,2,6
    [3, 4, 5, 7],   # right column: rows 0-3 → braille bits 3,4,5,7
]


def _render_world_map(selected_offset_h: int, selected_offset_m: int,
                      highlighted_tz_indices: list[int],
                      map_cols: int, map_rows: int,
                      current_tz_idx: int = -1) -> list[str]:
    """Render a sub-pixel braille world map.

    Each terminal character cell samples 2×4 = 8 geographic points (braille
    dots), giving smooth continental coastlines instead of blocky filled tiles.
    """
    bitmap = _get_world_bitmap()
    src_h = len(bitmap)
    src_w = len(bitmap[0])

    # Equirectangular projection, aspect-ratio corrected for terminal cells.
    # CHAR_ASPECT ≈ 2: each terminal cell is ~2× taller than wide, so we
    # need to show twice as many latitude degrees per row as longitude per col.
    CHAR_ASPECT = 2.0
    lat_range = min(180.0, 360.0 * map_rows * CHAR_ASPECT / map_cols)
    lat_range = max(lat_range, 160.0)   # always reach at least ±80° so Antarctica shows
    lat_top = lat_range / 2.0

    # Pixel grid: each char = 2 px wide × 4 px tall
    px_w = map_cols * 2
    px_h = map_rows * 4

    def _px_to_lonlat(px_col: int, px_row: int):
        """Map a sub-pixel (2x4 per char cell) coordinate to (lon, lat)."""
        lon = -180.0 + (px_col + 0.5) * 360.0 / px_w
        lat = lat_top - (px_row + 0.5) * lat_range / px_h
        return lon, lat

    def _sample_px(px_col: int, px_row: int) -> int:
        """Coastline bitmap bit at this sub-pixel; 0 outside ±90° latitude."""
        lon, lat = _px_to_lonlat(px_col, px_row)
        if lat < -90 or lat > 90:
            return 0
        sc = int((lon + 180.0) / 360.0 * src_w)
        sr = int((90.0 - lat) / 180.0 * src_h)
        return bitmap[min(max(sr, 0), src_h - 1)][min(sc, src_w - 1)]

    def _char_lon(char_col: int) -> float:
        """Longitude at the center of a map character column."""
        return -180.0 + (char_col + 0.5) * 360.0 / map_cols

    def _char_lat(char_row: int) -> float:
        """Latitude at the center of a map character row."""
        return lat_top - (char_row + 0.5) * lat_range / map_rows

    # Timezone raster lookup (1440×720, byte = round((utc_offset+12)*4), 255=ocean)
    tzmap = _get_tzmap()
    TZ_W, TZ_H = 1440, 720
    selected_zone = selected_offset_h + selected_offset_m / 60.0
    selected_code = int(round((selected_zone + 12) * 4))
    def _is_hl_ll(lat: float, lon: float) -> bool:
        """Whether (lat, lon) falls in the currently selected UTC offset's timezone."""
        tc = min(TZ_W - 1, max(0, int((lon + 180.0) / 360.0 * TZ_W)))
        tr = min(TZ_H - 1, max(0, int((90.0 - lat) / 180.0 * TZ_H)))
        code = tzmap[tr * TZ_W + tc]
        return code != 255 and code == selected_code

    def _is_hl(char_col: int, char_row: int) -> bool:
        """Whether a map character cell falls in the currently selected offset's timezone."""
        return _is_hl_ll(_char_lat(char_row), _char_lon(char_col))

    def _lat_to_crow(lat: float) -> int:
        """Latitude to map character row."""
        return int((lat_top - lat) / lat_range * map_rows)

    def _lon_to_ccol(lon: float) -> int:
        """Longitude to map character column."""
        return int((lon + 180.0) / 360.0 * map_cols)

    # Build dot maps
    # all_cap_cells: cell → True if also a highlighted tz, False if global capital only
    cur_tz_cell: tuple[int, int] | None = None
    if current_tz_idx >= 0:
        _, _, clat, clon, _, _, _, _ = _TIMEZONES[current_tz_idx]
        cr, cc = _lat_to_crow(clat), _lon_to_ccol(clon)
        if 0 <= cr < map_rows and 0 <= cc < map_cols:
            cur_tz_cell = (cr, cc)

    # Capitals in the current offset (bright dots)
    hl_cap_cells: set[tuple[int, int]] = set()
    for idx in highlighted_tz_indices:
        entry = _TIMEZONES[idx]
        if not entry[7]:
            continue
        dr = _lat_to_crow(entry[2])
        dc = _lon_to_ccol(entry[3])
        if 0 <= dr < map_rows and 0 <= dc < map_cols:
            hl_cap_cells.add((dr, dc))

    eq_row = _lat_to_crow(0.0)
    pm_col = _lon_to_ccol(0.0)

    LAND_FG    = "\033[38;2;80;160;80m"
    HL_LAND    = "\033[38;2;160;255;160m"
    RESET      = "\033[0m"
    DOT_FG     = "\033[38;2;200;190;80m"   # same-offset capital: bright yellow
    DOT_CUR_FG = "\033[1;97m"             # selected city: bright white bold
    DIM_FG     = "\033[38;2;45;75;45m"

    lines = []
    for char_row in range(map_rows):
        line = ""
        base_py = char_row * 4
        for char_col in range(map_cols):
            hl = _is_hl(char_col, char_row)
            cell = (char_row, char_col)
            is_eq  = (char_row == eq_row)
            is_pm  = (char_col == pm_col)

            if cell == cur_tz_cell:
                line += DOT_CUR_FG + "◉" + RESET
                continue
            if cell in hl_cap_cells:
                line += DOT_FG + "•" + RESET
                continue

            # Sample the 8 braille sub-pixels (2 cols × 4 rows)
            base_px = char_col * 2
            bits = 0
            any_land = False
            for dc in range(2):
                for dr in range(4):
                    if _sample_px(base_px + dc, base_py + dr):
                        bits |= (1 << _BRAILLE_BIT[dc][dr])
                        any_land = True

            if any_land:
                ch = chr(0x2800 + bits)
                line += (HL_LAND if hl else LAND_FG) + ch + RESET
            elif is_eq or is_pm:
                marker = "┼" if (is_eq and is_pm) else ("─" if is_eq else "│")
                line += DIM_FG + marker + RESET
            else:
                line += " "

        lines.append(line)

    return lines


def timezone_select(initial_offset: str = "") -> str | None:
    """
    Full-screen interactive timezone picker.

    Navigation:
        ←/→: cycle UTC offsets
        ↑/↓: cycle timezones within current offset
        Type: search filter
        ESC (search empty): cancel, return None
        ESC (search non-empty): clear search
        BACKSPACE (search non-empty): clear search
        ENTER: confirm, return offset string
        q/Q: quit (_state.QUIT_REQUESTED = True), return current offset string

    Returns:
        "Z" for UTC+0, "+05:30" for UTC+5:30, "-08:00" for UTC-8, or None on cancel.
    """
    def _parse_initial(s: str) -> tuple[int, int]:
        """Parse an offset string like "+05:30" or "Z" into (hour, minute); default (0, 0)."""
        if not s or s == "Z":
            return (0, 0)
        m = re.match(r'^([+-])(\d{1,2}):(\d{2})$', s)
        if m:
            sign = 1 if m.group(1) == '+' else -1
            return (sign * int(m.group(2)), int(m.group(3)))
        return (0, 0)

    init_h, init_m = _parse_initial(initial_offset)

    unique_offsets = sorted(set((tz[0], tz[1]) for tz in _TIMEZONES))

    try:
        off_idx = unique_offsets.index((init_h, init_m))
    except ValueError:
        off_idx = unique_offsets.index((0, 0)) if (0, 0) in unique_offsets else 0

    cur_offset = unique_offsets[off_idx]

    def _tzs_for_offset(oh: int, om: int) -> list[int]:
        """Indices into _TIMEZONES sharing this UTC offset."""
        return [i for i, tz in enumerate(_TIMEZONES) if tz[0] == oh and tz[1] == om]

    tz_idx_in_offset = 0
    zone_viewport    = 0   # top visible zone index in the list
    search_str = ""

    MAX_ZONE_ROWS = 4   # max zone list rows shown at once

    def _offset_str(oh: int, om: int) -> str:
        """Format (hour, minute) as the widget's return value: "Z" or "±HH:MM"."""
        if oh == 0 and om == 0:
            return "Z"
        sign = "+" if oh >= 0 else "-"
        return f"{sign}{abs(oh):02d}:{om:02d}"

    def _offset_display(oh: int, om: int) -> str:
        """Format (hour, minute) as a human-readable "UTC±HH:MM" label."""
        if oh == 0 and om == 0:
            return "UTC+00:00"
        sign = "+" if oh >= 0 else "-"
        return f"UTC{sign}{abs(oh):02d}:{om:02d}"

    def _tz_desc(idx: int) -> str:
        """Display label for a timezone entry: "City (ABBR)"."""
        tz = _TIMEZONES[idx]
        return f"{tz[6]} ({tz[5]})"

    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)
    w   = _Widget(fd)

    def _render():
        nonlocal zone_viewport
        cols = _cols()
        rows = _visible_rows()
        lines = []

        oh, om = cur_offset
        tzs = _tzs_for_offset(oh, om)
        cur_tz_idx = tzs[tz_idx_in_offset] if tzs else -1
        n_zones = len(tzs)

        # Keep viewport tracking the selection
        vis = min(MAX_ZONE_ROWS, n_zones)
        if tz_idx_in_offset < zone_viewport:
            zone_viewport = tz_idx_in_offset
        elif tz_idx_in_offset >= zone_viewport + vis:
            zone_viewport = tz_idx_in_offset - vis + 1

        # ── Offset navigator strip ──────────────────────────────────────────
        # Show up to 2 neighbours each side: ‹ UTC-5 · UTC-4:30 · [UTC-4] · UTC-3:30 · UTC-3 ›
        NEIGHBORS = 2
        strip_parts = []
        lo = max(0, off_idx - NEIGHBORS)
        hi = min(len(unique_offsets) - 1, off_idx + NEIGHBORS)
        if lo > 0:
            strip_parts.append(f"{C.DIM}‹{C.RESET}")
        for i in range(lo, hi + 1):
            label = _offset_display(*unique_offsets[i])
            if i == off_idx:
                strip_parts.append(f"{C.BOLD}{C.PRIMARY}{label}{C.RESET}")
            else:
                strip_parts.append(f"{C.DIM}{label}{C.RESET}")
        if hi < len(unique_offsets) - 1:
            strip_parts.append(f"{C.DIM}›{C.RESET}")
        strip = f"  {('  ·  ').join(strip_parts)}"

        lines.append(strip)

        if search_str:
            lines.append(f"  {C.DIM}search:{C.RESET}  {search_str}{C.DIM}█{C.RESET}")

        # ── Zone list ───────────────────────────────────────────────────────
        zone_rows_emitted = 0
        if n_zones == 0:
            lines.append(f"  {C.DIM}(no zones for this offset){C.RESET}")
            lines.append("")
            zone_rows_emitted = 2
        else:
            for rank in range(zone_viewport, min(zone_viewport + vis, n_zones)):
                tidx     = tzs[rank]
                selected = (rank == tz_idx_in_offset)
                pointer  = f"{C.ACCENT}›{C.RESET}" if selected else " "
                name     = _tz_desc(tidx)
                if selected:
                    name_s = f"{C.BOLD}{name}{C.RESET}"
                else:
                    name_s = f"{C.DIM}{name}{C.RESET}"
                count_s = ""
                if selected and n_zones > 1:
                    count_s = f"  {C.DIM}{tz_idx_in_offset + 1}/{n_zones}{C.RESET}"
                lines.append(f"  {pointer} {name_s}{count_s}")
                zone_rows_emitted += 1
            # Pad to MAX_ZONE_ROWS so map never shifts
            while zone_rows_emitted < MAX_ZONE_ROWS:
                lines.append("")
                zone_rows_emitted += 1

        # Hint bar
        hint = _hint(("←→", "offset"), ("↑↓", "zone"), ("↵", "confirm"), ("esc", "cancel/clear"))
        hint_lines = hint.splitlines()
        hint_line_count = len(hint_lines)

        # Map fills remaining rows — search line is always 1 slot (blank when empty)
        fixed_rows = 1 + 1 + MAX_ZONE_ROWS + hint_line_count  # strip + search + zones + hints
        if not search_str:
            lines.insert(1, "")  # blank placeholder keeps map stable
        map_rows = max(4, rows - fixed_rows)
        map_cols = cols

        map_lines = _render_world_map(oh, om, tzs, map_cols, map_rows, cur_tz_idx)
        lines.extend(map_lines)

        lines.extend(hint_lines)

        w.render(lines)

    result = None
    try:
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        sys.stdout.write("\033[H\033[3J\033[J")
        sys.stdout.flush()
        _render()

        while True:
            if ui_utils.consume_resize():
                sys.stdout.write("\033[H\033[3J\033[J")
                sys.stdout.flush()
                w.anchor_reset()
                _render()
                continue

            if not _wait_for_keypress(0.05):
                continue

            key = _read_key(fd)

            if key in ('CTRL_C',):
                break

            if key == 'ESC':
                if search_str:
                    search_str = ""
                    _render()
                else:
                    break

            elif key == 'BACKSPACE':
                if search_str:
                    search_str = search_str[:-1]
                    _render()

            elif key == 'ENTER':
                oh, om = cur_offset
                result = _offset_str(oh, om)
                break

            elif key == 'RIGHT':
                off_idx = (off_idx + 1) % len(unique_offsets)
                cur_offset = unique_offsets[off_idx]
                tz_idx_in_offset = 0
                zone_viewport = 0
                search_str = ""
                _render()

            elif key == 'LEFT':
                off_idx = (off_idx - 1) % len(unique_offsets)
                cur_offset = unique_offsets[off_idx]
                tz_idx_in_offset = 0
                zone_viewport = 0
                search_str = ""
                _render()

            elif key == 'DOWN':
                tzs = _tzs_for_offset(*cur_offset)
                if tzs:
                    tz_idx_in_offset = (tz_idx_in_offset + 1) % len(tzs)
                _render()

            elif key == 'UP':
                tzs = _tzs_for_offset(*cur_offset)
                if tzs:
                    tz_idx_in_offset = (tz_idx_in_offset - 1) % len(tzs)
                _render()

            elif len(key) == 1 and (key.isalpha() or key.isdigit() or key in '+-_/ '):
                search_str += key
                q = search_str.lower()
                for i, tz in enumerate(_TIMEZONES):
                    if (q in tz[4].lower() or q in tz[5].lower() or
                            q in tz[6].lower()):
                        oh2, om2 = tz[0], tz[1]
                        if (oh2, om2) in unique_offsets:
                            idx2 = unique_offsets.index((oh2, om2))
                            off_idx = idx2
                            cur_offset = unique_offsets[off_idx]
                            tzs2 = _tzs_for_offset(oh2, om2)
                            tz_idx_in_offset = tzs2.index(i) if i in tzs2 else 0
                            zone_viewport = 0
                            break
                _render()

    finally:
        sys.stdout.write("\033[?1000l\033[?1006l")
        _restore_term_attrs(fd, old)
        w.clear()

    return result


if __name__ == '__main__':
    # Standalone demo: `python3 src/utils/tz_widget.py [+05:30]`
    _initial = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        _picked = timezone_select(_initial)
    except QuitToTerminal:
        _picked = None
    print(f"Selected offset: {_picked}" if _picked is not None else "Cancelled.")


