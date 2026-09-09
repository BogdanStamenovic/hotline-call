"""Word error rate for the Serbian telephony benchmark.

Two numbers on purpose. `raw` is what the model literally emitted. `norm`
additionally spells out digit strings, because "16" for "sesnaest" is a
formatting choice rather than a mishearing and counting it as an error would
flatter a model that happens to write words instead of numerals.

Diacritics are NOT stripped: č/ć and dž/đ are the distinction the whole test
exists to measure.

`translit` is a THIRD number and it exists because the first run of this scorer
nearly threw away two working models. `samil24-small-serbian` and
`drishtisharma-medium-serbian` answer in CYRILLIC, and against a Latin reference
that scores 96-101% WER -- which reads as a broken model and is nothing of the
kind. Serbian is officially digraphic; both scripts are correct Serbian and the
choice between them is a formatting decision exactly like "16" for "šesnaest".
Folding Cyrillic to Latin preserves every distinction this test measures, because
ђ->đ and џ->dž are one-to-one. Judge a model on what it HEARD.
"""
import json
import re
import sys
import unicodedata

# Serbian Cyrillic to Latin, one to one. Same table cvoice's asr.py uses, minus
# its diacritic stripping -- that would erase the contrasts being measured.
_CYR = "абвгдђежзијклљмнњопрстћуфхцчџш"
_LAT = ["a", "b", "v", "g", "d", "đ", "e", "ž", "z", "i", "j", "k", "l", "lj",
        "m", "n", "nj", "o", "p", "r", "s", "t", "ć", "u", "f", "h", "c", "č",
        "dž", "š"]


def latin(s):
    return "".join(_LAT[_CYR.index(c)] if c in _CYR else c for c in s)

# Small numbers only, and NOTHING keyed on the values that happened to appear in
# one reference. The first version of this table hardcoded the digit groups from
# his actual phone number, which this repo -- being public -- must not carry.
# It was also overfitting: a lookup tuned to one script measures that script.
UNITS = ["nula", "jedan", "dva", "tri", "četiri", "pet", "šest", "sedam", "osam",
         "devet", "deset", "jedanaest", "dvanaest", "trinaest", "četrnaest",
         "petnaest", "šesnaest", "sedamnaest", "osamnaest", "devetnaest"]
TENS = {20: "dvadeset", 30: "trideset", 40: "četrdeset", 50: "pedeset",
        60: "šezdeset", 70: "sedamdeset", 80: "osamdeset", 90: "devedeset"}


def spell(token):
    """A digit token as a person would say it.

    Anything longer than two digits is read out digit by digit, because that is
    how anyone dictates a number down a phone -- and it is what the model is
    being asked to have heard. Two digits or fewer get the ordinary numeral.
    """
    n = int(token)
    if len(token) > 2:
        return [UNITS[int(d)] for d in token]
    if n < 20:
        return [UNITS[n]]
    if n % 10 == 0 and n in TENS:
        return [TENS[n]]
    if n < 100:
        return [TENS[n - n % 10], UNITS[n % 10]]
    return [token]

def words(s, spell_digits, translit=False):
    s = unicodedata.normalize("NFC", s).lower()
    if translit:
        s = latin(s)
    s = re.sub(r"[.,!?;:\"'()]", " ", s)
    out = []
    for w in s.split():
        if spell_digits and w.isdigit():
            out.extend(spell(w))
        else:
            out.append(w)
    return out

def wer(ref, hyp):
    d = [[0]*(len(hyp)+1) for _ in range(len(ref)+1)]
    for i in range(len(ref)+1): d[i][0] = i
    for j in range(len(hyp)+1): d[0][j] = j
    for i in range(1, len(ref)+1):
        for j in range(1, len(hyp)+1):
            d[i][j] = min(d[i-1][j]+1, d[i][j-1]+1,
                          d[i-1][j-1] + (ref[i-1] != hyp[j-1]))
    return d[len(ref)][len(hyp)], len(ref)

ref_doc = json.load(open(sys.argv[1]))
hyps = json.load(open(sys.argv[2]))     # {"turn": "text"}
lines = ref_doc["lines"]; mapping = ref_doc["turn_to_lines"]

METRICS = (("raw", False, False), ("norm", True, False), ("translit", True, True))
tot = {name: [0, 0] for name, _, _ in METRICS}
print(f"{'turn':<5} {'lines':<7} {'raw':>7} {'norm':>7} {'translit':>9}   reference / hypothesis")
for t in sorted(mapping, key=int):
    if t not in hyps: continue
    reference = " ".join(lines[i-1] for i in mapping[t])
    hypothesis = hyps[t]
    row = []
    for key, digits, tr in METRICS:
        e, n = wer(words(reference, digits, tr), words(hypothesis, digits, tr))
        tot[key][0] += e; tot[key][1] += n
        row.append(f"{100*e/n:6.1f}%")
    print(f"{t:<5} {mapping[t]!s:<7} {row[0]:>7} {row[1]:>7} {row[2]:>9}")
    print(f"      REF  {reference}")
    print(f"      HYP  {hypothesis}")
print()
for key, _, _ in METRICS:
    e, n = tot[key]
    print(f"TOTAL {key:<5} {100*e/n:5.1f}%  ({e} errors / {n} reference words)")
