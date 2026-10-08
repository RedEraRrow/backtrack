"""Every ISO 639-2 language code (both the B and T forms where they differ),
the codes an ID3 COMM or USLT frame takes."""

_ALL = """
aar Afar|abk Abkhazian|ace Achinese|ach Acoli|ada Adangme|ady Adyghe|afa Afro-Asiatic languages
afh Afrihili|afr Afrikaans|ain Ainu|aka Akan|akk Akkadian|alb Albanian|sqi Albanian|ale Aleut
alg Algonquian languages|alt Southern Altai|amh Amharic|ang Old English|anp Angika|apa Apache languages
ara Arabic|arc Aramaic|arg Aragonese|arm Armenian|hye Armenian|arn Mapudungun|arp Arapaho
art Artificial languages|arw Arawak|asm Assamese|ast Asturian|ath Athapascan languages
aus Australian languages|ava Avaric|ave Avestan|awa Awadhi|aym Aymara|aze Azerbaijani
bad Banda languages|bai Bamileke languages|bak Bashkir|bal Baluchi|bam Bambara|ban Balinese
baq Basque|eus Basque|bas Basa|bat Baltic languages|bej Beja|bel Belarusian|bem Bemba|ben Bengali
ber Berber languages|bho Bhojpuri|bih Bihari languages|bik Bikol|bin Bini|bis Bislama|bla Siksika
bnt Bantu languages|bos Bosnian|bra Braj|bre Breton|btk Batak languages|bua Buriat|bug Buginese
bul Bulgarian|bur Burmese|mya Burmese|byn Blin|cad Caddo|cai Central American Indian languages
car Galibi Carib|cat Catalan|cau Caucasian languages|ceb Cebuano|cel Celtic languages|cha Chamorro
chb Chibcha|che Chechen|chg Chagatai|chi Chinese|zho Chinese|chk Chuukese|chm Mari|chn Chinook jargon
cho Choctaw|chp Chipewyan|chr Cherokee|chu Church Slavic|chv Chuvash|chy Cheyenne|cmc Chamic languages
cnr Montenegrin|cop Coptic|cor Cornish|cos Corsican|cpe English-based creoles|cpf French-based creoles
cpp Portuguese-based creoles|cre Cree|crh Crimean Tatar|crp Creoles and pidgins|csb Kashubian
cus Cushitic languages|cze Czech|ces Czech|dak Dakota|dan Danish|dar Dargwa|day Land Dayak languages
del Delaware|den Slave (Athapascan)|dgr Dogrib|din Dinka|div Divehi|doi Dogri|dra Dravidian languages
dsb Lower Sorbian|dua Duala|dum Middle Dutch|dut Dutch|nld Dutch|dyu Dyula|dzo Dzongkha|efi Efik
egy Ancient Egyptian|eka Ekajuk|elx Elamite|eng English|enm Middle English|epo Esperanto|est Estonian
ewe Ewe|ewo Ewondo|fan Fang|fao Faroese|fat Fanti|fij Fijian|fil Filipino|fin Finnish
fiu Finno-Ugrian languages|fon Fon|fre French|fra French|frm Middle French|fro Old French
frr Northern Frisian|frs Eastern Frisian|fry Western Frisian|ful Fulah|fur Friulian|gaa Ga|gay Gayo
gba Gbaya|gem Germanic languages|geo Georgian|kat Georgian|ger German|deu German|gez Geez
gil Gilbertese|gla Scottish Gaelic|gle Irish|glg Galician|glv Manx|gmh Middle High German
goh Old High German|gon Gondi|gor Gorontalo|got Gothic|grb Grebo|grc Ancient Greek|gre Greek|ell Greek
grn Guarani|gsw Swiss German|guj Gujarati|gwi Gwich'in|hai Haida|hat Haitian|hau Hausa|haw Hawaiian
heb Hebrew|her Herero|hil Hiligaynon|him Himachali languages|hin Hindi|hit Hittite|hmn Hmong
hmo Hiri Motu|hrv Croatian|hsb Upper Sorbian|hun Hungarian|hup Hupa|iba Iban|ibo Igbo|ice Icelandic
isl Icelandic|ido Ido|iii Sichuan Yi|ijo Ijo languages|iku Inuktitut|ile Interlingue|ilo Iloko
ina Interlingua|inc Indic languages|ind Indonesian|ine Indo-European languages|inh Ingush|ipk Inupiaq
ira Iranian languages|iro Iroquoian languages|ita Italian|jav Javanese|jbo Lojban|jpn Japanese
jpr Judeo-Persian|jrb Judeo-Arabic|kaa Kara-Kalpak|kab Kabyle|kac Kachin|kal Kalaallisut|kam Kamba
kan Kannada|kar Karen languages|kas Kashmiri|kau Kanuri|kaw Kawi|kaz Kazakh|kbd Kabardian|kha Khasi
khi Khoisan languages|khm Khmer|kho Khotanese|kik Kikuyu|kin Kinyarwanda|kir Kyrgyz|kmb Kimbundu
kok Konkani|kom Komi|kon Kongo|kor Korean|kos Kosraean|kpe Kpelle|krc Karachay-Balkar|krl Karelian
kro Kru languages|kru Kurukh|kua Kuanyama|kum Kumyk|kur Kurdish|kut Kutenai|lad Ladino|lah Lahnda
lam Lamba|lao Lao|lat Latin|lav Latvian|lez Lezghian|lim Limburgish|lin Lingala|lit Lithuanian
lol Mongo|loz Lozi|ltz Luxembourgish|lua Luba-Lulua|lub Luba-Katanga|lug Ganda|lui Luiseno|lun Lunda
luo Luo|lus Mizo|mac Macedonian|mkd Macedonian|mad Madurese|mag Magahi|mah Marshallese|mai Maithili
mak Makasar|mal Malayalam|man Mandingo|mao Maori|mri Maori|map Austronesian languages|mar Marathi
mas Masai|may Malay|msa Malay|mdf Moksha|mdr Mandar|men Mende|mga Middle Irish|mic Mi'kmaq
min Minangkabau|mis Uncoded languages|mkh Mon-Khmer languages|mlg Malagasy|mlt Maltese|mnc Manchu
mni Manipuri|mno Manobo languages|moh Mohawk|mon Mongolian|mos Mossi|mul Several languages
mun Munda languages|mus Creek|mwl Mirandese|mwr Marwari|myn Mayan languages|myv Erzya
nah Nahuatl languages|nai North American Indian languages|nap Neapolitan|nau Nauru|nav Navajo
nbl South Ndebele|nde North Ndebele|ndo Ndonga|nds Low German|nep Nepali|new Newari|nia Nias
nic Niger-Kordofanian languages|niu Niuean|nno Norwegian Nynorsk|nob Norwegian Bokmål|nog Nogai
non Old Norse|nor Norwegian|nqo N'Ko|nso Northern Sotho|nub Nubian languages|nwc Classical Newari
nya Chichewa|nym Nyamwezi|nyn Nyankole|nyo Nyoro|nzi Nzima|oci Occitan|oji Ojibwa|ori Odia|orm Oromo
osa Osage|oss Ossetian|ota Ottoman Turkish|oto Otomian languages|paa Papuan languages|pag Pangasinan
pal Pahlavi|pam Pampanga|pan Punjabi|pap Papiamento|pau Palauan|peo Old Persian|per Persian|fas Persian
phi Philippine languages|phn Phoenician|pli Pali|pol Polish|pon Pohnpeian|por Portuguese
pra Prakrit languages|pro Old Provençal|pus Pashto|que Quechua|raj Rajasthani|rap Rapanui
rar Rarotongan|roa Romance languages|roh Romansh|rom Romany|rum Romanian|ron Romanian|run Rundi
rup Aromanian|rus Russian|sad Sandawe|sag Sango|sah Yakut|sai South American Indian languages
sal Salishan languages|sam Samaritan Aramaic|san Sanskrit|sas Sasak|sat Santali|scn Sicilian|sco Scots
sel Selkup|sem Semitic languages|sga Old Irish|sgn Sign languages|shn Shan|sid Sidamo|sin Sinhala
sio Siouan languages|sit Sino-Tibetan languages|sla Slavic languages|slo Slovak|slk Slovak
slv Slovenian|sma Southern Sami|sme Northern Sami|smi Sami languages|smj Lule Sami|smn Inari Sami
smo Samoan|sms Skolt Sami|sna Shona|snd Sindhi|snk Soninke|sog Sogdian|som Somali
son Songhai languages|sot Southern Sotho|spa Spanish|srd Sardinian|srn Sranan Tongo|srp Serbian
srr Serer|ssa Nilo-Saharan languages|ssw Swati|suk Sukuma|sun Sundanese|sus Susu|sux Sumerian
swa Swahili|swe Swedish|syc Classical Syriac|syr Syriac|tah Tahitian|tai Tai languages|tam Tamil
tat Tatar|tel Telugu|tem Timne|ter Tereno|tet Tetum|tgk Tajik|tgl Tagalog|tha Thai|tib Tibetan
bod Tibetan|tig Tigre|tir Tigrinya|tiv Tiv|tkl Tokelau|tlh Klingon|tli Tlingit|tmh Tamashek
tog Tonga (Nyasa)|ton Tongan|tpi Tok Pisin|tsi Tsimshian|tsn Tswana|tso Tsonga|tuk Turkmen
tum Tumbuka|tup Tupi languages|tur Turkish|tut Altaic languages|tvl Tuvalu|twi Twi|tyv Tuvinian
udm Udmurt|uga Ugaritic|uig Uyghur|ukr Ukrainian|umb Umbundu|und Undetermined|urd Urdu|uzb Uzbek
vai Vai|ven Venda|vie Vietnamese|vol Volapük|vot Votic|wak Wakashan languages|wal Wolaitta|war Waray
was Washo|wel Welsh|cym Welsh|wen Sorbian languages|wln Walloon|wol Wolof|xal Kalmyk|xho Xhosa
yao Yao|yap Yapese|yid Yiddish|yor Yoruba|ypk Yupik languages|zap Zapotec|zbl Blissymbols|zen Zenaga
zgh Standard Moroccan Tamazight|zha Zhuang|zul Zulu|zun Zuni|zxx No words|zza Zaza
"""

# The commonest first, then the rest by code.
_FIRST = ('eng', 'fre', 'ger', 'spa', 'ita', 'por', 'dut', 'swe', 'nor', 'dan', 'fin', 'pol', 'cze',
          'hun', 'gre', 'rus', 'ukr', 'tur', 'ara', 'heb', 'hin', 'jpn', 'chi', 'kor', 'gle', 'wel',
          'lat', 'mul', 'und', 'zxx')

_names = dict(entry.split(" ", 1) for entry in _ALL.replace("\n", "|").split("|") if entry)
LANGUAGES = tuple((c, _names[c]) for c in (*_FIRST, *sorted(set(_names) - set(_FIRST))))
CODES = frozenset(_names)
