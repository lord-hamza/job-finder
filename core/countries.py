"""Countries by name, dialling code and best-known places. Used to tell which country a CV is for, and where a job is,
without asking Gemini. Best effort: a CV's country can always be corrected in Setup."""
import re

# ITU dialling codes. +1 and +7 are shared, so the area code decides those.
_DIAL = {
    "1": "United States", "7": "Russia", "20": "Egypt", "27": "South Africa", "30": "Greece", "31": "Netherlands",
    "32": "Belgium", "33": "France", "34": "Spain", "36": "Hungary", "39": "Italy", "40": "Romania",
    "41": "Switzerland", "43": "Austria", "44": "United Kingdom", "45": "Denmark", "46": "Sweden", "47": "Norway",
    "48": "Poland", "49": "Germany", "51": "Peru", "52": "Mexico", "53": "Cuba", "54": "Argentina", "55": "Brazil",
    "56": "Chile", "57": "Colombia", "58": "Venezuela", "60": "Malaysia", "61": "Australia", "62": "Indonesia",
    "63": "Philippines", "64": "New Zealand", "65": "Singapore", "66": "Thailand", "81": "Japan",
    "82": "South Korea", "84": "Vietnam", "86": "China", "850": "North Korea", "90": "Turkey", "91": "India", "92": "Pakistan",
    "93": "Afghanistan", "94": "Sri Lanka", "95": "Myanmar", "98": "Iran", "211": "South Sudan", "212": "Morocco",
    "213": "Algeria", "216": "Tunisia", "218": "Libya", "220": "Gambia", "221": "Senegal", "222": "Mauritania",
    "223": "Mali", "224": "Guinea", "225": "Ivory Coast", "226": "Burkina Faso", "227": "Niger", "228": "Togo",
    "229": "Benin", "230": "Mauritius", "231": "Liberia", "232": "Sierra Leone", "233": "Ghana", "234": "Nigeria",
    "235": "Chad", "236": "Central African Republic", "237": "Cameroon", "238": "Cape Verde",
    "239": "Sao Tome and Principe", "240": "Equatorial Guinea", "241": "Gabon", "242": "Republic of the Congo",
    "243": "DR Congo", "244": "Angola", "245": "Guinea-Bissau", "248": "Seychelles", "249": "Sudan",
    "250": "Rwanda", "251": "Ethiopia", "252": "Somalia", "253": "Djibouti", "254": "Kenya", "255": "Tanzania",
    "256": "Uganda", "257": "Burundi", "258": "Mozambique", "260": "Zambia", "261": "Madagascar",
    "263": "Zimbabwe", "264": "Namibia", "265": "Malawi", "266": "Lesotho", "267": "Botswana", "268": "Eswatini",
    "269": "Comoros", "291": "Eritrea", "350": "Gibraltar", "351": "Portugal", "352": "Luxembourg",
    "353": "Ireland", "354": "Iceland", "355": "Albania", "356": "Malta", "357": "Cyprus", "358": "Finland",
    "359": "Bulgaria", "370": "Lithuania", "371": "Latvia", "372": "Estonia", "373": "Moldova", "374": "Armenia",
    "375": "Belarus", "376": "Andorra", "377": "Monaco", "378": "San Marino", "380": "Ukraine", "381": "Serbia",
    "382": "Montenegro", "383": "Kosovo", "385": "Croatia", "386": "Slovenia", "387": "Bosnia and Herzegovina",
    "389": "North Macedonia", "420": "Czech Republic", "421": "Slovakia", "423": "Liechtenstein",
    "501": "Belize", "502": "Guatemala", "503": "El Salvador", "504": "Honduras", "505": "Nicaragua",
    "506": "Costa Rica", "507": "Panama", "509": "Haiti", "591": "Bolivia", "592": "Guyana", "593": "Ecuador",
    "595": "Paraguay", "597": "Suriname", "598": "Uruguay", "673": "Brunei", "675": "Papua New Guinea",
    "676": "Tonga", "677": "Solomon Islands", "678": "Vanuatu", "679": "Fiji", "685": "Samoa",
    "852": "Hong Kong", "853": "Macau", "855": "Cambodia", "856": "Laos", "880": "Bangladesh", "886": "Taiwan",
    "960": "Maldives", "961": "Lebanon", "962": "Jordan", "963": "Syria", "964": "Iraq", "965": "Kuwait",
    "966": "Saudi Arabia", "967": "Yemen", "968": "Oman", "970": "Palestine", "971": "United Arab Emirates",
    "972": "Israel", "973": "Bahrain", "974": "Qatar", "975": "Bhutan", "976": "Mongolia", "977": "Nepal",
    "992": "Tajikistan", "993": "Turkmenistan", "994": "Azerbaijan", "995": "Georgia", "996": "Kyrgyzstan",
    "998": "Uzbekistan",
}
# +1 area codes outside the United States. US territories such as Puerto Rico and Guam count as the United States.
_NANP = {
    **dict.fromkeys(
        "204 226 236 249 250 257 263 289 306 343 354 365 367 368 382 403 416 418 428 431 437 438 450 460 468 474 "
        "506 514 519 548 579 581 584 587 604 613 639 647 672 683 705 709 742 753 778 780 782 807 819 825 867 873 "
        "879 902 905 942".split(),
        "Canada",
    ),
    "242": "Bahamas", "246": "Barbados", "264": "Anguilla", "268": "Antigua and Barbuda",
    "284": "British Virgin Islands", "345": "Cayman Islands", "441": "Bermuda", "473": "Grenada",
    "649": "Turks and Caicos Islands", "658": "Jamaica", "876": "Jamaica", "664": "Montserrat",
    "721": "Sint Maarten", "758": "Saint Lucia", "767": "Dominica", "784": "Saint Vincent and the Grenadines",
    "809": "Dominican Republic", "829": "Dominican Republic", "849": "Dominican Republic",
    "868": "Trinidad and Tobago", "869": "Saint Kitts and Nevis",
}

# Other ways a country is written, and its best-known cities. Names that mean more than one place are left out:
# Georgia is also a US state, Hyderabad is in India and in Pakistan, and Perth is in Australia and in Scotland.
# Names in capitals, such as US, only count in capitals, so "us" in a sentence isn't the United States.
_ALSO = {
    "Pakistan": "Lahore, Karachi, Islamabad, Rawalpindi, Faisalabad, Multan, Peshawar, Quetta, Sialkot, Gujranwala",
    "United Kingdom": (
        "UK, U.K., Great Britain, Britain, England, Scotland, Wales, Northern Ireland, London, Manchester, "
        "Birmingham, Leeds, Liverpool, Glasgow, Edinburgh, Bristol, Sheffield, Cardiff, Belfast, Leicester, "
        "Nottingham, Coventry, Bradford, Southampton, Brighton, Milton Keynes"
    ),
    "United States": (
        "US, U.S., U.S, USA, U.S.A., United States of America, New England, New York, NYC, San Francisco, "
        "Los Angeles, Chicago, Seattle, Boston, Atlanta, Miami, Denver, San Diego, San Jose, Philadelphia, "
        "Silicon Valley, Alabama, Alaska, Arizona, Arkansas, California, Colorado, Connecticut, Delaware, Florida, "
        "Hawaii, Idaho, Illinois, Indiana, Iowa, Kansas, Kentucky, Louisiana, Maine, Maryland, Massachusetts, "
        "Michigan, Minnesota, Mississippi, Missouri, Montana, Nebraska, Nevada, New Hampshire, New Jersey, "
        "New Mexico, North Carolina, North Dakota, Ohio, Oklahoma, Oregon, Pennsylvania, Rhode Island, "
        "South Carolina, South Dakota, Tennessee, Texas, Utah, Vermont, Virginia, Washington, West Virginia, "
        "Wisconsin, Wyoming"
    ),
    "Canada": "Toronto, Vancouver, Montreal, Montréal, Calgary, Ottawa, Edmonton, Winnipeg, Ontario, Quebec, British Columbia, Alberta",
    "United Arab Emirates": "UAE, U.A.E., Dubai, Abu Dhabi, Sharjah, Ajman",
    "Saudi Arabia": "KSA, Riyadh, Jeddah, Dammam, Khobar",
    "Qatar": "Doha",
    "Bahrain": "Manama",
    "Oman": "Muscat",
    "India": "Bangalore, Bengaluru, Mumbai, Delhi, New Delhi, Pune, Chennai, Kolkata, Noida, Gurgaon, Gurugram, Ahmedabad",
    "Bangladesh": "Dhaka, Chittagong",
    "Sri Lanka": "Colombo",
    "Nepal": "Kathmandu",
    "Ireland": "Republic of Ireland, Dublin, Cork, Galway",
    "Germany": "Deutschland, Berlin, Munich, München, Hamburg, Frankfurt, Cologne, Stuttgart",
    "Netherlands": "The Netherlands, Holland, Amsterdam, Rotterdam, Utrecht, Eindhoven, The Hague",
    "France": "Paris, Lyon, Marseille, Toulouse",
    "Spain": "Madrid, Barcelona, Valencia",
    "Italy": "Rome, Milan",
    "Portugal": "Lisbon, Porto",
    "Poland": "Warsaw, Krakow, Kraków, Wroclaw",
    "Sweden": "Stockholm, Gothenburg",
    "Switzerland": "Zurich, Zürich, Geneva",
    "Belgium": "Brussels, Antwerp",
    "Austria": "Vienna",
    "Denmark": "Copenhagen",
    "Norway": "Oslo",
    "Finland": "Helsinki",
    "Czech Republic": "Czechia, Prague",
    "Australia": "Sydney, Melbourne, Brisbane, Canberra, New South Wales, Queensland",
    "New Zealand": "Auckland, Wellington, Christchurch",
    "Singapore": "",
    "Malaysia": "Kuala Lumpur",
    "Philippines": "Manila, Cebu",
    "Indonesia": "Jakarta",
    "Thailand": "Bangkok",
    "Vietnam": "Hanoi, Ho Chi Minh City",
    "China": "Beijing, Shanghai, Shenzhen, Guangzhou",
    "Japan": "Tokyo, Osaka",
    "South Korea": "Korea, Seoul",
    "Nigeria": "Lagos, Abuja",
    "Kenya": "Nairobi",
    "South Africa": "Johannesburg, Cape Town, Pretoria, Durban",
    "Egypt": "Cairo, Alexandria",
    "Ghana": "Accra",
    "Turkey": "Türkiye, Turkiye, Istanbul, Ankara",
    "Brazil": "São Paulo, Sao Paulo, Rio de Janeiro",
    "Mexico": "Mexico City",
    "Argentina": "Buenos Aires",
    "Ivory Coast": "Côte d'Ivoire, Cote d'Ivoire",
    "DR Congo": "Democratic Republic of the Congo, Kinshasa",
    "Eswatini": "Swaziland",
    "North Macedonia": "Macedonia",
    "Myanmar": "Burma",
    "Georgia": "Tbilisi",
    "Kazakhstan": "Almaty, Astana",
}
_NOT_ON_ITS_OWN = {"Georgia"}  # the country's name is also a US state's

NAMES = sorted(set(_DIAL.values()) | set(_NANP.values()) | set(_ALSO))


def _aliases():
    found = {name: name for name in NAMES if name not in _NOT_ON_ITS_OWN}
    for name, others in _ALSO.items():
        for alias in filter(None, (part.strip() for part in others.split(","))):
            found[alias] = name
    return found


_ALIAS = _aliases()
_LOOKUP = {(alias if not re.search(r"[a-z]", alias) else alias.casefold()): name for alias, name in _ALIAS.items()}
_PLACE = re.compile(
    r"(?<![\w.@/-])(?:"
    + "|".join(
        re.escape(alias) if not re.search(r"[a-z]", alias) else f"(?i:{re.escape(alias)})"
        for alias in sorted(_ALIAS, key=len, reverse=True)  # "Northern Ireland" before "Ireland"
    )
    + r")(?![\w@/-])"
)
# "Austin, TX 78701". A state with a ZIP code is certain. A bare two-letter code is only a hint, since other countries'
# regions use the same letters ("Coimbatore, TN" is in India); those that are also degrees or country codes, such as
# "Jane Doe, MD" or "Berlin, DE", don't count at all without a ZIP code.
_US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR"
    "|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY"
)
_US_STATE = re.compile(rf",\s*(?:{_US_STATES})\s+\d{{5}}(?:-\d{{4}})?\b")
_US_STATE_HINT = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CO|CT|DC|FL|GA|HI|ID|IL|IA|KS|KY|LA|MI|MN|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|RI|SC|SD|TN|TX|UT"
    r"|VT|VA|WA|WV|WI|WY)\b(?![.@/-]\w)(?!\s+\d{5})"
)
_CA_POSTCODE = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z] ?\d[ABCEGHJ-NPRSTV-Z]\d\b")
_CA_PROVINCE_HINT = re.compile(r",\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b(?![.@/-]\w)")
_UK_POSTCODE = re.compile(r"\b[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]? ?\d[ABD-HJLNP-UW-Z]{2}\b")


def canonical(name):
    """The usual name for a country however it's written, such as "UK" or "u.k." for "United Kingdom". Text that
    isn't a known country stays as it is."""
    name = " ".join((name or "").split()).strip(" ,.")
    found = _LOOKUP.get(name) or _LOOKUP.get(name.casefold()) or _LOOKUP.get(name.upper())
    if not found and name.upper().replace(".", "") in ("UK", "US", "USA", "UAE", "KSA"):
        found = _LOOKUP.get(name.upper().replace(".", ""))
    return found or name


def key(name):
    """Compares countries however they're written."""
    return canonical(name).casefold()


def same(a, b):
    return bool(a and b) and key(a) == key(b)


def places(text, hints=True):
    """Countries the text names, by country, other names, cities, US states, Canadian provinces or UK postcodes.
    A list of (position, country) in order of appearance. hints: also bare state and province codes, as in "Austin, TX"."""
    text = text or ""
    found = [(match.start(), _LOOKUP.get(match.group()) or _LOOKUP.get(match.group().casefold())) for match in _PLACE.finditer(text)]
    found += [(match.start(), "United States") for match in _US_STATE.finditer(text)]
    found += [(match.start(), "Canada") for match in _CA_POSTCODE.finditer(text)]
    found += [(match.start(), "United Kingdom") for match in _UK_POSTCODE.finditer(text)]
    if hints:
        found += hinted(text)
    return sorted((start, country) for start, country in found if country)


def hinted(text):
    """Countries only a bare two-letter region code suggests: (position, country)."""
    found = [(match.start(), "United States") for match in _US_STATE_HINT.finditer(text or "")]
    return found + [(match.start(), "Canada") for match in _CA_PROVINCE_HINT.finditer(text or "")]


_ANYWHERE = re.compile(
    r"^\W*(worldwide|world-?wide|anywhere|anywhere in the world|global|globally|any country|any location|international"
    r"|internationally|all countries|everywhere|no limit|none|n/?a|remote)\W*$",
    re.I,
)
_KNOWN = {name.casefold() for name in NAMES}


def resolve(value):
    """The countries a short piece of text names, however it's written: "the UK", "Lahore, Pakistan" and "UK residents
    only" each name one. Nothing for "worldwide" and the like, and anything else, such as a region, as it is."""
    value = " ".join((value or "").split()).strip(" .,;:")
    if not value or _ANYWHERE.match(value):
        return []
    if canonical(value).casefold() in _KNOWN:
        return [canonical(value)]
    found = list(dict.fromkeys(country for _, country in places(value)))
    return found or [value]


def first_place(text):
    found = places(text)
    return found[0][1] if found else ""


def only_place(text):
    """The country the text names, if it names just one. "London, ON" names two, so it's left to other signs."""
    found = {country for _, country in places(text)}
    return found.pop() if len(found) == 1 else ""


def from_phone(phone):
    """(country, how sure) for a phone number. 2 when it carries its country code, 1 for a local format that one
    country is known for, ("", 0) otherwise."""
    phone = (phone or "").strip()
    digits = re.sub(r"\D", "", phone)
    international = phone.lstrip("(").startswith("+") or (phone.startswith("00") and len(digits) > 11)
    if international:
        digits = digits[2:] if phone.startswith("00") else digits
        if digits.startswith("1") and len(digits) == 11:
            return _NANP.get(digits[1:4], "United States"), 2
        if digits.startswith("7"):
            return ("Kazakhstan" if digits[1:2] in ("6", "7") else "Russia"), 2
        for size in (3, 2):
            if digits[:size] in _DIAL:
                return _DIAL[digits[:size]], 2
        return "", 0
    if re.fullmatch(r"03\d{9}", digits):
        return "Pakistan", 1
    if re.fullmatch(r"07\d{9}", digits):
        return "United Kingdom", 1
    if re.fullmatch(r"(?:1[ .-]?)?\(?[2-9]\d{2}\)?[ .-]?[2-9]\d{2}[ .-]?\d{4}", phone):  # (650) 555-0123: only North America
        return _NANP.get(digits[-10:-7], "United States"), 2
    return "", 0
