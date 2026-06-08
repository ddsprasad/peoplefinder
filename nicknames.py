"""Common English given-name nickname groups, for query-time name expansion.

Each group is a set of interchangeable forms. `NICKNAMES` maps every lowercase
name to the full set of variants in its group (including itself), so a search
for "Robert" in nickname mode can also match "Bob", "Bobby", "Rob", etc.

This list is intentionally editable — add a row to GROUPS to teach the audit a
new nickname; no reindexing is required (expansion happens in the query).
"""

# Each inner list is one interchangeable group. Keep entries lowercase.
GROUPS = [
    ["abigail", "abby", "abbie", "gail"],
    ["alexander", "alex", "al", "xander", "sandy"],
    ["alexandra", "alex", "alexa", "lexi", "sandra", "sandy"],
    ["andrew", "andy", "drew"],
    ["anthony", "tony", "ant"],
    ["benjamin", "ben", "benny", "benji"],
    ["bernard", "bernie"],
    ["bradley", "brad"],
    ["catherine", "cathy", "kate", "katie", "cat", "katherine", "kathy", "kat"],
    ["charles", "charlie", "chuck", "chas", "chip"],
    ["christopher", "chris", "topher", "kit"],
    ["christine", "chris", "chrissy", "tina"],
    ["daniel", "dan", "danny"],
    ["david", "dave", "davey"],
    ["deborah", "deb", "debbie", "debra"],
    ["dennis", "denny"],
    ["donald", "don", "donnie"],
    ["dorothy", "dot", "dottie", "dolly"],
    ["douglas", "doug"],
    ["edward", "ed", "eddie", "ted", "ted", "ned"],
    ["elizabeth", "liz", "lizzie", "beth", "betty", "betsy", "eliza", "libby", "lisa"],
    ["frances", "fran", "frankie"],
    ["francis", "frank", "frankie"],
    ["frederick", "fred", "freddie", "fritz"],
    ["gerald", "gerry", "jerry"],
    ["gregory", "greg"],
    ["harold", "harry", "hal"],
    ["henry", "hank", "harry", "hal"],
    ["jacob", "jake"],
    ["james", "jim", "jimmy", "jamie", "jem"],
    ["janet", "jan"],
    ["jennifer", "jen", "jenny", "jenn"],
    ["jeffrey", "jeff"],
    ["jessica", "jess", "jessie"],
    ["john", "johnny", "jack", "jon"],
    ["jonathan", "jon", "jonny", "nathan"],
    ["joseph", "joe", "joey"],
    ["joshua", "josh"],
    ["katherine", "kate", "katie", "kathy", "kat", "catherine", "cathy"],
    ["kenneth", "ken", "kenny"],
    ["kimberly", "kim"],
    ["lawrence", "larry", "lars"],
    ["leonard", "leo", "len", "lenny"],
    ["louis", "lou", "louie"],
    ["margaret", "maggie", "meg", "peggy", "marge", "greta", "rita"],
    ["matthew", "matt", "matty"],
    ["megan", "meg"],
    ["michael", "mike", "mikey", "mick", "mitch"],
    ["nicholas", "nick", "nicky", "cole"],
    ["pamela", "pam"],
    ["patricia", "pat", "patty", "trish", "tricia"],
    ["patrick", "pat", "paddy", "rick"],
    ["peter", "pete"],
    ["philip", "phil", "philippe"],
    ["rebecca", "becky", "becca", "bec"],
    ["richard", "rick", "dick", "rich", "richie", "ricky"],
    ["robert", "bob", "bobby", "rob", "robbie", "bert"],
    ["ronald", "ron", "ronnie"],
    ["russell", "russ"],
    ["samuel", "sam", "sammy"],
    ["samantha", "sam", "sammy"],
    ["stephen", "steve", "steph"],
    ["steven", "steve", "steph"],
    ["susan", "sue", "susie", "suzy"],
    ["theodore", "ted", "teddy", "theo"],
    ["thomas", "tom", "tommy", "thom"],
    ["timothy", "tim", "timmy"],
    ["victoria", "vicky", "vic", "tori"],
    ["virginia", "ginny", "ginger"],
    ["walter", "walt", "wally"],
    ["william", "will", "bill", "billy", "willy", "liam"],
    ["zachary", "zach", "zack"],
]


def _build_lookup():
    lookup = {}
    for group in GROUPS:
        names = set(n.lower() for n in group)
        for n in names:
            lookup.setdefault(n, set()).update(names)
    return lookup


# name (lowercase) -> set of all interchangeable variants (incl. itself).
NICKNAMES = _build_lookup()
