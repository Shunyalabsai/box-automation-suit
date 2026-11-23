#!/usr/bin/env python3
"""Replay the partner P0/P1 scorecard against the live bot and score it deterministically.

Why this exists: audit finding F22 — the existing eval scores single messages, so it cannot see the
failures that actually break the product. This drives real multi-turn conversations through
/api/test/*, which runs every turn through processIncomingMessageFull (the exact production path a
WhatsApp webhook takes), and scores the failure modes the audit measured.

Every check is deterministic. There is no LLM judge here on purpose: the audit found ours cannot see
replies at all (F25), so a judge would add cost and no signal. Checks are intentionally strict-but-
cheap; a case that passes every check is not "good", it is "did not fail in a way we know is broken".

    python3 eval/run_partner_p0.py --out eval/reports/partner_p0_<label>.json
"""
import argparse, base64, errno, fcntl, json, os, re, subprocess, sys, time
import urllib.request, urllib.error, uuid

# THE DEFAULT USED TO BE http://127.0.0.1:8100, AND THAT IS THE OLD PRODUCT.
#
# 127.0.0.1:8100 is `boxx_app`, the Go-era system, and boxxv2.shunyalabs.ai proxies to it. This build
# is `boxxpy_app` on 127.0.0.1:8200 and is reachable only at boxxv3.shunyalabs.ai. So every default
# here, every example in the docs and every report that says boxxv2 was measuring a different product,
# and the numbers taken that way are not weak evidence about this branch, they are evidence about
# another one. `assert_target_is_our_build()` below refuses to run rather than let that happen again.
BASE = os.environ.get("BOXX_TEST_BASE", "https://boxxv3.shunyalabs.ai")
KEY = os.environ.get("TEST_API_KEY", "")

# ---- PACING (revalidation round 3 §3) -----------------------------------------------------------
#
# Round 3 opened with both suites reporting PYTHON_PIPELINE_ERROR on 2 cases each — a fail mode that
# did not exist in round 2. It was not a code regression: the same cases ran clean in isolation, the
# intelligence container logged HTTP 429 during the window, and the author had triggered it by running
# probes CONCURRENTLY WITH A SUITE. Re-running mined_production alone gave 0 pipeline errors and a
# better score (4/9 vs 3/9).
#
# That is the worst thing a gate can do. A gate that fails for reasons unrelated to the product is how
# people learn to ignore a gate, and this one failed on the suite carrying the only real traffic in the
# repo. Three things are needed and all three are here rather than in the Makefile, because the
# Makefile only drives CI and the failure was caused by a human at a shell:
#
#   1. AN EXCLUSIVE RUN LOCK. The Test API is already strictly sequential — testAPIMaxConcurrentTurns
#      = 1, internal/handlers/test_api.go:68 — and sheds anything that waits more than 5s with a 429.
#      A turn takes up to 11s (measured), so a second caller is not "slower", it is SHED. The lock
#      makes that structurally impossible rather than a rule people remember. `flock` on the same file
#      works from a shell, so an ad-hoc probe can join the same discipline:
#          flock eval/.suite-run.lock curl ... /api/test/session/$SID/message
#   2. A COOLDOWN BETWEEN RUNS. Back-to-back suites sustain load on the shared upstream past the point
#      where the per-request backoff can absorb it. The lock file's mtime records when the last run
#      finished, so the next one paces itself without the caller having to remember to sleep.
#   3. RETRY, THEN LABEL. See _post() and reclassify_throttled_cases(): a 429 is not a verdict.
SUITE_LOCK = os.environ.get("BOXX_EVAL_LOCK",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".suite-run.lock"))
# Long enough that a whole suite can queue behind another suite rather than dying — CI runs them in
# series and the point is that the second one WAITS instead of manufacturing failures.
LOCK_WAIT_S = float(os.environ.get("BOXX_EVAL_LOCK_WAIT_S", "1800"))
# Measured against the observed window: the 429s in round 3 appeared while a suite was still draining
# and cleared within seconds of it stopping. 20s is cheap next to a suite and is the whole cost of
# this fix per run.
COOLDOWN_S = float(os.environ.get("BOXX_EVAL_COOLDOWN_S", "20"))


class SuiteLock:
    """An exclusive, advisory, cross-process lock around one suite run, plus the inter-run cooldown.

    Advisory and file-based on purpose: `flock(1)` takes the same lock, so a shell probe and a Python
    suite serialise against each other without either knowing about the other. Held for the WHOLE run
    (not per turn), because the failure mode is sustained load, not a single collision.

    Never fails the run. If the lock cannot be taken — no filesystem, no fcntl, a stale holder — the
    run proceeds and says so. A pacing mechanism that can block a measurement is worse than the
    throttling it prevents.
    """

    def __init__(self, path=SUITE_LOCK, wait_s=LOCK_WAIT_S, cooldown_s=COOLDOWN_S, enabled=True):
        self.path, self.wait_s, self.cooldown_s, self.enabled = path, wait_s, cooldown_s, enabled
        self.fh = None
        self.waited_s = 0.0
        self.cooled_s = 0.0
        self.note = ""

    def __enter__(self):
        if not self.enabled:
            self.note = "lock disabled (--no-lock)"
            return self
        try:
            self.fh = open(self.path, "a+")
        except OSError as e:
            self.note = f"lock unavailable ({e}) — running unpaced"
            print(f"WARNING: {self.note}", file=sys.stderr)
            return self
        t0, announced = time.time(), False
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    self.note = f"lock unavailable ({e}) — running unpaced"
                    print(f"WARNING: {self.note}", file=sys.stderr)
                    self.fh = None
                    return self
                if not announced:
                    print(f"PACING: another eval run holds {self.path}; waiting (a concurrent run is "
                          f"what manufactured round 3's PYTHON_PIPELINE_ERRORs)", file=sys.stderr)
                    announced = True
                if time.time() - t0 > self.wait_s:
                    self.note = f"lock wait exceeded {self.wait_s:.0f}s — running unpaced"
                    print(f"WARNING: {self.note}", file=sys.stderr)
                    self.fh = None
                    return self
                time.sleep(2)
        self.waited_s = round(time.time() - t0, 1)
        # The cooldown is measured from when the PREVIOUS holder released, which is what the mtime
        # records: whoever finishes touches the file. Waiting for the lock already counts toward it,
        # so a run that queued for ten minutes does not then sleep another twenty seconds.
        try:
            since = time.time() - os.path.getmtime(self.path)
            remaining = self.cooldown_s - since
            if remaining > 0:
                print(f"PACING: {remaining:.0f}s cooldown after the previous run", file=sys.stderr)
                time.sleep(remaining)
                self.cooled_s = round(remaining, 1)
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        if self.fh is None:
            return False
        try:
            os.utime(self.path, None)          # stamp the release — this is what the cooldown reads
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
        except OSError:
            pass
        return False

# --- DuckDB traceability -------------------------------------------------------------------------
# This harness wrote ONLY a JSON file, which meant the conversation eval — the one that grades the
# multi-turn product — was invisible to every dashboard, trend and delta the single-message eval
# already had. The OLAP client is imported rather than reimplemented: its retry/backoff/spool
# behaviour is the reason a row cannot be silently lost, and a second copy of that logic would be a
# second thing to get wrong.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
OLAP_IMPORT_ERROR = ""
try:
    from olap_client import OlapClient, git_sha, olap_enabled
except Exception as exc:  # httpx missing (the system python3 has no venv), stripped checkout, ...
    OLAP_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    OlapClient = None

    def olap_enabled():
        # DELIBERATELY still reads the env var. If someone sets EVAL_OLAP=1 and the client cannot
        # be imported, that is a BROKEN run, not a disabled one, and olap_start says so loudly.
        # Hard-coding False here would turn a missing dependency into a silent no-op, which is how
        # a run that nobody can trace ends up looking exactly like a run nobody wanted to trace.
        return os.environ.get("EVAL_OLAP", "").strip().lower() in {"1", "true", "yes", "on"}

    def git_sha():
        return ""

# QS-04: the English-direction language grader. Imported, never inlined — it parses the production
# lexicons out of internal/handlers/*.go so this harness and the shipping guard cannot disagree about
# whether a sentence is English. Guarded because a stripped checkout without internal/ must still be
# able to run the delivery gates rather than dying at import; a missing grader is reported LOUDLY,
# because "the check did not run" and "the check found nothing" looking alike is the whole of QS-01.
try:
    import lang_parity
except Exception as _exc:                       # noqa: BLE001 — any parse/IO failure is the same story
    lang_parity = None
    print(f"WARNING: lang_parity unavailable ({type(_exc).__name__}: {_exc}) — the QS-04 "
          f"English-direction check is NOT RUNNING and this run cannot certify language",
          file=sys.stderr)

# The suite this process is running. It is the file's own basename, resolved in main() from
# --suite, NOT a constant: Makefile.eval drives six different suite files through this one script,
# and a hardcoded name would stamp every one of them "partner_p0" in DuckDB. The dashboard slices
# and filters by `suite`, so that would silently merge six populations into one label — and a
# regression in media_v1 would appear as a regression in partner_p0.
SUITE_NAME = "partner_p0"


def suite_name_from_path(path: str) -> str:
    """`eval/suites/conversation_dynamics.jsonl` -> `conversation_dynamics`."""
    base = os.path.basename(str(path or "")).strip()
    for ext in (".jsonl", ".json"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return base or "partner_p0"

CRISIS = re.compile(r"14416|1-?800-?891-?4416|iCALL|9152987821|\b112\b|Tele-?MANAS", re.I)
HANDOFF = re.compile(
    r"call you back|speak with (our|the) team|callback request|team will call|"
    r"received your request to speak|"
    # THE ACK COPY ITSELF. HANDOFF and HANDOFF_ACK below are two regexes for one concept, and they had
    # drifted: the live ack — "I'm connecting you to a member of our team. They'll call you on this
    # WhatsApp number" — matched HANDOFF_ACK and NOT this, so a correctly honoured handoff was scored
    # HANDOFF_NOT_HONOURED. A grader disagreeing with itself about the same sentence is the failure mode
    # that has cost the most time in this project; the ack alternatives are duplicated here deliberately
    # so the containment (ack ⊂ handoff) holds by construction.
    # Deliberately NOT a bare "they will call you": this same regex drives SPURIOUS_HANDOFF, and a
    # scam warning ("they will call you again pretending to be from the bank") would then score a
    # correct reply as an unwanted handoff. Anchored on the ack's own distinguishing phrase instead.
    r"connecting you to a member of our team|connect kar rahi hoon|"
    r"call you on this( whatsapp)? number|"
    # THE HONEST HANDOFF, which this pattern refused to recognise and which the product is now
    # REQUIRED to produce. Every alternative above promises a telephone call: "call you back", "team
    # will call", "call you on this whatsapp number". `check.no_false_claim` bans exactly that,
    # because we cannot make a call, and `act` is told "NEVER say anyone will telephone them".
    #
    # So the grader demanded a sentence the product's own safety check forbids. Measured 2026-08-17:
    # the reply "I'm going to pass this chat to one of our team members now, and they will reply to
    # you here in this conversation" honours the request completely, promises nothing we cannot do,
    # and scored HANDOFF_NOT_HONOURED on three cases. Two parts of our own system disagreeing about
    # the same sentence is the failure mode this file has already been bitten by twice.
    #
    # Anchored on handing the CONVERSATION over, which a scam warning never says, so SPURIOUS_HANDOFF
    # is not loosened: "they will call you again pretending to be from the bank" still does not match.
    r"pass(ing)? (this|the) (chat|conversation|case) (on |over )?to|"
    r"hand(ing)? (this|the) (chat|conversation|case) (on |over )?to|"
    r"(team member|colleague|someone from our team|a person|human) will (reply|respond|pick)|"
    r"will reply to you here|reply to you in this (chat|conversation)|"
    r"chat (ko )?(hamari |our )?team (ko|se) (de|bhej|transfer)|"
    r"team ka koi (member|vyakti) (aapko )?(yahin |isi chat mein )?(reply|jawab)|"
    # The insurer-deflection shape (partner P0, insurance claim): "contact BOXX/the insurer directly,
    # details are in your policy documents". Scored as a handoff because from the user's seat it is one —
    # the chat did not take the claim. English-only matching missed it entirely in Tamil, which is how
    # a known-open gap read as a pass.
    # Scoped to the DEFLECTION ("go to them, not me"), not to the legitimate "confirm against your
    # policy document" disclosure — that footer is correct behaviour and matching it flagged four
    # correct replies as handoffs.
    # NOT bare "them": "contact them directly using the official number from their website" is CORRECT
    # advice for a phishing SMS — verify through official channels — and matching it scored a good reply
    # as a handoff. The deflection this is looking for names the INSURER.
    r"contact (boxx|the insurer|your insurer) directly|"
    r"(claim|policy) (details|information) (are|is) (usually )?(available |found )?(in|on) (your|the) "
    r"(policy document|official website)|"
    r"நேரடியாக (boxx|பாக்ஸ்)|সরাসরি যোগাযোগ|"
    # ---- GRADER BUG, FIXED 2026-08-15. THE CONTAINMENT ASSERTED ABOVE WAS NEVER TRUE. ----
    #
    # The paragraph above says the ack alternatives are duplicated here "so the containment
    # (ack ⊂ handoff) holds by construction". It holds for the three ENGLISH and Hinglish
    # alternatives that were duplicated and for nothing else. Executed against the product's own
    # authored copy on 2026-08-15:
    #
    #     turn._HANDOFF_ACK["en"]      HANDOFF True    HANDOFF_ACK True
    #     turn._HANDOFF_ACK["hing"]    HANDOFF True    HANDOFF_ACK True
    #     turn._HANDOFF_ACK["hi"]      HANDOFF FALSE   HANDOFF_ACK False
    #     turn._ESCALATION_OFFER["hi"] HANDOFF FALSE   HANDOFF_ACK False
    #     a Bangla ack                 HANDOFF FALSE   HANDOFF_ACK TRUE     <- ack ⊄ handoff
    #
    # Two separate defects in one pattern:
    #
    #  1. THERE IS NO DEVANAGARI IN IT AT ALL. `_HANDOFF_ACK["hi"]` is reviewed, shipped copy that
    #     honours a handoff perfectly, and every case asserting `handoff_ok` in Hindi scored
    #     HANDOFF_NOT_HONOURED whatever the product did. TSV-42 is a Hindi handoff case. The grader
    #     could not have passed it.
    #  2. HANDOFF_ACK reaches bn/ta/te/pa/mr and this does not, so the containment is false in five
    #     languages: the same reply can score DUPLICATE_HANDOFF_ACK (the ack fired twice) and
    #     HANDOFF_NOT_HONOURED (no handoff found) in one run. That is the exact self-contradiction
    #     the paragraph above says it fixed, still live in every language it was not measured in.
    #
    # Fixed by making the containment structural instead of asserted: every HANDOFF_ACK alternative
    # is inlined below from the same source, and `test_grader_handoff_containment` re-derives the
    # relation on every run so a future edit to one cannot silently drop the other. The Devanagari
    # alternatives are read off the product's authored strings, not invented here.
    # Hinglish is the third gap in the same list and it was found by the same probe:
    # `_ESCALATION_OFFER["hing"]` says "team ka ek member aapko isi WhatsApp number par call
    # karega", which none of the English alternatives above reach either.
    #
    # NOT a bare "hamari team". Re-graded against 3,219 stored conversations in eval/reports, that
    # alternative matched the DATA RIGHTS copy - "main ise hamari team ke kisi vyakti tak pahuncha
    # dungi", the route for a deletion request - on three cases that had offered no handoff at all.
    # Same discipline as the "they will call you" note above: anchored on the escalation copy's own
    # distinguishing phrase, not on the word "team".
    r"team (ka|ke) ek member|priority par hamari team|whatsapp number par call kar|"
    r"हमारी टीम[^।\n]{0,40}(कॉल|संपर्क|जोड़)|टीम के एक सदस्य|टीम का एक सदस्य|"
    r"प्राथमिकता पर हमारी टीम|इसी whatsapp नंबर पर कॉल|आपको कॉल करेंग|आपको कॉल करेगा|"
    r"আমাদের টিম[^।\n]{0,30}(কল|যোগাযোগ)|টিমের সাথে যুক্ত|"
    r"எங்கள் குழு[^.\n]{0,30}(அழைப்|தொடர்பு)|குழுவுடன் இணை|"
    r"మా బృందం[^.\n]{0,30}(కాల్|సంప్రదిస్)|బృందానికి కనెక్ట్|"
    r"ਸਾਡੀ ਟੀਮ[^।\n]{0,30}(ਕਾਲ|ਸੰਪਰਕ)|ਟੀਮ ਨਾਲ ਜੋੜ|"
    r"आमची टीम[^।\n]{0,30}(कॉल|संपर्क)", re.I)
# Urgency must be measurable in every language we serve, or the harness under-measures precisely the
# languages the audit says are broken (F13) — a measurement bias pointing the same way as the defect.
# Native verbs for hang-up / block / call-the-bank / report, plus the digit-normalised helplines.
URGENT = re.compile(
    # Tolerant of the words a real reply puts between the verb and the object. "immediately block your
    # bank account, card, or UPI service" is urgent action, and a pattern that needed "block your card"
    # exactly scored it as none.
    r"1930|hang up|disconnect|call (your |the )?bank|"
    r"block\s+(?:\w+\s+){0,3}(card|account|upi|sim|number)|"
    r"freeze\s+(?:\w+\s+){0,3}(card|account|transaction)|freeze|"
    r"stop (any )?further (loss|transaction)|"
    r"phone rakh|band kar|turant|abhi block|bank ko call|"
    r"फ़ोन रख|फोन रख|तुरंत|बैंक को कॉल|ब्लॉक कर|बंद कर|"
    r"ফোন রেখে|অবিলম্বে|ব্যাঙ্ক|ব্লক কর|বন্ধ কর|"
    r"போனை வை|உடனே|உடனடியாக|வங்கிக்கு|பிளாக்|தடு|"
    r"వెంటనే|బ్యాంక్|బ్లాక్|"
    r"ਤੁਰੰਤ|ਬੈਂਕ|ਬਲਾਕ|"
    r"ताबडतोब|बँकेला", re.I)

# Indic digit shapes for 0-9, so a helpline written as ১৯৩০ / १९३० / ௧௯௩௦ is scored as the number it is.
_DIGITS = {}
for _base in (0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66):  # deva..malayalam
    for _d in range(10):
        _DIGITS[chr(_base + _d)] = str(_d)
_DIGIT_TABLE = str.maketrans(_DIGITS)


def normalise_digits(text: str) -> str:
    """Fold Indic numerals to ASCII before any numeric check.

    A Bengali reply gave the cybercrime helpline as ১৯৩০. That is the correct number and a reader can
    use it — but every numeric check in this harness, and every grounded-phone check in the product,
    matches ASCII. Without this fold, a correct urgent reply scores as no urgent action, and the
    ungrounded-number guard cannot see a number it should be validating.
    """
    return (text or "").translate(_DIGIT_TABLE)
# Script ranges for every language the product serves, not just the two that were easy.
#
# The harness asserted script for hi and ta only, so bn/te/mr/pa replies were UNMEASURED for language —
# and those are precisely the languages the audit says are structurally broken beyond one turn (F13) and
# have no real training data at all. A gate that skips the weakest languages certifies the strongest
# ones, which is the same measurement bias that let ASCII-only urgency checks score correct Tamil
# replies as failures.
#
# Marathi shares Devanagari with Hindi, so a script check cannot tell them apart. That is stated rather
# than papered over: for mr the check proves the reply is not English, which is the failure mode that
# actually occurred, and does not attempt to prove it is Marathi rather than Hindi.
SCRIPTS = {
    "hi": ("Devanagari", re.compile(r"[ऀ-ॿ]")),
    "mr": ("Devanagari", re.compile(r"[ऀ-ॿ]")),
    "ta": ("Tamil", re.compile(r"[஀-௿]")),
    "bn": ("Bangla", re.compile(r"[ঀ-৿]")),
    "te": ("Telugu", re.compile(r"[ఀ-౿]")),
    "pa": ("Gurmukhi", re.compile(r"[਀-੿]")),
    "kn": ("Kannada", re.compile(r"[ಀ-೿]")),
    "ml": ("Malayalam", re.compile(r"[ഀ-ൿ]")),
    "gu": ("Gujarati", re.compile(r"[઀-૿]")),
    "or": ("Odia", re.compile(r"[଀-୿]")),
}
DEVA = SCRIPTS["hi"][1]
TAMIL = SCRIPTS["ta"][1]

# Romanised codes must NOT contain their parent script: a bnro user types Bangla words in Latin, and a
# reply in Bangla script is as wrong for them as English is for a bn user.
ROMANISED = {"hing": "hi", "bnro": "bn", "taro": "ta", "tamro": "ta", "tero": "te",
             "telro": "te", "mrro": "mr", "paro": "pa"}


# The AI-assistant disclaimer is boilerplate sent once per conversation, and it happens to contain
# "call your bank first". Scoring it as evidence of urgent action let a case where the bot asked "has
# any money already left your account?" — of a user whose screenshot said "Rs.24,500.00 debited" —
# score as URGENT. A check that can be satisfied by boilerplate is not measuring the product.
#
# QS-02: THIS STRIPPER WAS DEAD. It matched `I'?m an AI assistant`, and the live copy has said
# "I'm the **boxx insurance** AI assistant" since the disclaimer was rewritten (user_messages.go:582).
# The Hinglish arm was dead the same way — `main ek AI assistant hoon` vs the live "main **boxx
# insurance ka** AI assistant hoon". So every reply since that rewrite has been graded WITH the
# preamble in it, and because the preamble contains "call your bank first" — which `URGENT` matches on
# `call (your |the )?bank` — a P0 active-loss case was being certified green on the preamble alone.
# The bot never had to say anything urgent to score URGENT.
#
# THE FIX IS NOT A BETTER REGEX, IT IS NOT KEEPING A SECOND COPY. A hand-transcribed disclaimer is a
# duplicate of a string that product owns and edits, and it will die again the next time someone
# reworders it — silently, in the direction of a false pass. The three authored variants are parsed out
# of `msgDefaultDisclaimer` in internal/handlers/user_messages.go at import time, so a rewording
# updates the grader in the same commit. `_assert_boilerplate_strips()` below fails loudly at import
# if that parse stops finding them, because a stripper that quietly matches nothing is precisely the
# defect being repaired.
_USER_MESSAGES_GO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "internal", "handlers", "user_messages.go")
# Where product owns that copy after the port. One constant, because three functions below read this
# same file and three copies of the path is three chances to follow only two of them next time.
_PY_CATALOGUE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "catalogue", "messages.py")


# 2026-08-04, W5/LEN-01: IT DIED AGAIN, EXACTLY AS PREDICTED, AND IN THE SAME DIRECTION.
#
# The paragraph above says a hand-transcribed disclaimer "will die again the next time someone
# rewords it — silently, in the direction of a false pass", and then pinned the parse to ONE function
# name. The 17-word budget work split the notice into two bubbles, so `msgDefaultDisclaimer` became
# `strings.Join(msgDefaultDisclaimerParts(lang), "\n\n")` — a function with no string literals in it.
# The parse returned [] and the stripper fell back to a landmark regex whose closing landmark
# ("matters more than this chat") the new copy no longer contains, so NOTHING was stripped: measured
# on eval/reports/w5_baseline, the three preamble sentences were the three most repeated sentences in
# the corpus at 30 occurrences each, and multi_bubble_rate read 43.1% where it is 5.4% with the
# preamble removed.
#
# So the parse now reads the PARTS function — the one that owns the literals — and falls back to the
# joined one for older checkouts. The landmark regexes are a LIST, one per bubble, because the notice
# is no longer a single run of text and a single pattern anchored on "AI assistant" can only ever
# reach the first half. The legacy pattern is kept: this grader reads historical reports, and a
# report written in July still carries the July wording.
def _live_disclaimers() -> list:
    """The disclaimer strings exactly as the product sends them, in every locale it authors.

    Both the individual bubbles AND their joined form, because a report may carry either: the
    product now sends two messages, and `msgDefaultDisclaimer` still assembles the one-string form
    for the transcript row and for presend_opener.go's byte-for-byte identity check.
    """
    # 2026-08-05: IT DIED A THIRD TIME, AND THE PARAGRAPH ABOVE PREDICTED THE SHAPE OF IT AGAIN.
    #
    # The Go tree was deleted when the product was ported to Python, so internal/handlers/user_messages.go
    # no longer exists at all and this returned [] on every run. Same consequence as last time, in the
    # same direction: nothing was stripped, the notice text was carried into the content checks, and a
    # case could be convicted of MUST_MATCH_MISSING because its pattern was sitting in the disclaimer
    # bubble rather than in the answer. Measured on the 16-case tablestake run, that produced at least
    # two false failures (TS-09 c3 and TS-15 c2), both tagged boilerplate-carried.
    #
    # So the parse follows the strings to where product owns them now: the Python catalogue in
    # backend/app/core/messages.py, keys default_disclaimer_part_1 and _part_2. Read as TEXT rather
    # than imported, deliberately, because this script runs on a bare python3 with no backend
    # dependencies installed, and an import would trade one silent [] for a hard crash.
    # The Go path is kept below it, because this grader also reads reports written before the port and
    # a July report still carries the July wording.
    #
    # 2026-08-05, AND THE COUNT ITSELF WAS THE FOURTH FAILURE. The joined-form rebuild below used
    # to slice one flat list under `len(out) >= 6 and len(out) % 3 == 0`, which is a hardcoded
    # "there are exactly three languages". The catalogue now authors THIRTEEN, 26 parts is not
    # divisible by 3, and the joined strippers stopped being built at all: the same silent
    # direction as the three failures above it. So the Python arm pairs part 1 with part 2 BY
    # LOCALE KEY and counts nothing. Thirteen locales, three, or thirty all work, and so does a
    # third part. Nothing in this function may ever again be conditional on how many there are.
    out = []
    try:
        with open(_PY_CATALOGUE, encoding="utf-8") as fh:
            py_src = fh.read()
    except OSError:
        py_src = ""
    py_parts = []                       # one {locale: text} per authored part, in part order
    # ALL FOUR AUTHORED PARTS, and two of them were missing. `default_disclaimer_golden_hour` has
    # been in the sent notice since 2026-08-17 and was never listed here, so the 1930 paragraph was
    # carried into the content checks as if the bot had said it — the exact boilerplate-carried false
    # failure this function's own history is a list of. `default_disclaimer_liability` joins it from
    # 2026-08-18. `_part_2` is kept because this grader also reads reports written before it was cut.
    for key in ("default_disclaimer_part_1", "default_disclaimer_part_2",
                "default_disclaimer_golden_hour", "default_disclaimer_liability"):
        m = re.search(r'"' + key + r'"\s*:\s*\{(.*?)\n\s*\},', py_src, re.S)
        if not m:
            continue
        by_locale = {loc: text for loc, text in
                     re.findall(r'"([a-z]{2,6})"\s*:\s*"((?:[^"\\]|\\.)*)"', m.group(1))
                     if len(text) > 40}
        py_parts.append(by_locale)
        out += list(by_locale.values())
    # The joined form, per locale. A locale that is missing a part is simply not joined, which is
    # the honest outcome: there is no such notice to strip.
    if len(py_parts) > 1:
        for loc in py_parts[0]:
            if all(loc in part for part in py_parts):
                out.append("\n\n".join(part[loc] for part in py_parts))

    try:
        with open(_USER_MESSAGES_GO, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        src = ""
    go = []
    for fn in (r"func msgDefaultDisclaimerParts\(lang string\) \[\]string \{(.*?)\n\}",
               r"func msgDefaultDisclaimer\(lang string\) string \{(.*?)\n\}"):
        m = re.search(fn, src, re.S)
        if not m:
            continue
        go += [s for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if len(s) > 40]
    out += go
    # The Go joined form, for reports written before the port. A Go switch carries no locale key to
    # pair on, so this arm stays positional: localizedByLang() listed en, hi, hing in that order,
    # so the parts interleave as [en1, hi1, hing1, en2, hi2, hing2]. It now slices ONLY the Go
    # literals, so what the Python catalogue authors can no longer decide whether it runs.
    if len(go) >= 6 and len(go) % 3 == 0:
        per_lang = len(go) // 3
        for lang_i in range(3):
            out.append("\n\n".join(go[p * 3 + lang_i] for p in range(per_lang)))
    return out


LIVE_DISCLAIMERS = _live_disclaimers()

# The reworded/translated arm. ta/bn/te/pa/mr disclaimers are produced by translating the English one
# at runtime, so there is no literal to match — but every variant keeps its landmarks. ONE PATTERN
# PER BUBBLE, since the notice is authored as two messages:
#
#   1. the AI self-identification, closing on "not legal or financial advice"
#   2. the never-share prohibition, closing on "call your bank first"
#
# Bubble 2 matters more than it looks. It ends on "call your bank first", and `URGENT` below matches
# `call (your |the )?bank` — which is the precise mechanism by which a P0 active-loss case was once
# certified green on the preamble alone, without the bot ever saying anything urgent. Left unstripped
# for the translated languages, that false pass comes straight back.
#
# Bounded, not `.*?` over an unbounded span: an over-reaching stripper deletes real product copy and
# turns a failing case green, which is the same defect from the other side.
#
# LEGACY GOES FIRST, and the ordering is load-bearing. Pattern 1 below also matches the opening of
# the July wording (both close on "financial advice"), and if it runs first it eats the head and
# leaves ", that matters more than this chat." stranded in the graded blob of every historical
# report. Longest match first is the general rule; here it is the only rule that leaves nothing.
BOILERPLATE_PATTERNS = [
    # 0 — LEGACY, the single-run July wording. Kept because this grader reads historical reports,
    # and every report under eval/reports/ written before 2026-08-04 carries it. It cannot match the
    # two-bubble form: that form has no "matters more than this chat" clause to close on.
    re.compile(
        r"(?:before we start|shuru karne se pehle|शुरू करने से पहले)?[^.।\n]{0,40}"
        r"(?:AI assistant|AI सहायक|AI\s+\w{0,12}assistant)"
        r".*?(?:matters more than this chat|zyada zaroori hai|ज़्यादा ज़रूरी है|ज्यादा जरूरी है)[.।]?",
        re.I | re.S),
    # 1 — AI self-identification .. "advice"
    re.compile(
        r"(?:quick note|ek baat|एक बात|before we start|shuru karne se pehle|शुरू करने से पहले)?"
        r"[^.।\n]{0,40}(?:AI assistant|AI सहायक|AI\s+\w{0,12}assistant)"
        r"[^\n]{0,200}?(?:financial advice|financial advice nahi|वित्तीय सलाह(?: नहीं)?|"
        r"nahi hai|advice nahi)[.।]?", re.I),
    # 2 — the never-share prohibition .. the bank-first clause
    re.compile(
        r"(?:never share|kabhi share na karein|कभी साझा न करें|OTP)"
        r"[^\n]{0,160}?(?:call your bank first|pehle bank ko call karein|"
        r"पहले बैंक को कॉल करें|zyada zaroori hai|ज़्यादा ज़रूरी है|ज्यादा जरूरी है)[.।]?", re.I),
]
# Kept as a name because other modules import it. The legacy single-run pattern, which is the one
# that historically WAS `BOILERPLATE`.
BOILERPLATE = BOILERPLATE_PATTERNS[0]


def strip_boilerplate(text: str) -> str:
    t = text or ""
    for d in LIVE_DISCLAIMERS:          # exact first: cannot over-reach, cannot under-match
        if d in t:
            t = t.replace(d, " ")
    for rx in BOILERPLATE_PATTERNS:
        t = rx.sub(" ", t)
    return t


def _assert_boilerplate_strips():
    """Import-time fence for QS-02. A stripper that no longer matches the live copy must not be able
    to fail silently — that failure mode cost a P0 case its only real assertion for weeks, and then
    did it a second time on 2026-08-04 when the notice was split into two bubbles."""
    if not LIVE_DISCLAIMERS:
        print("WARNING: could not parse default_disclaimer_part_1/_part_2 from "
              "backend/app/core/messages.py (nor the legacy Go source). The boilerplate stripper "
              "is running on the landmark regexes alone (QS-02)", file=sys.stderr)
        return
    for d in LIVE_DISCLAIMERS:
        left = strip_boilerplate(d).strip()
        if left:
            print(f"WARNING (QS-02): the disclaimer is not fully stripped; {len(left)} chars survive "
                  f"into the graded blob: {left[:120]!r}", file=sys.stderr)


_assert_boilerplate_strips()


# ---- mechanism naming (F23) --------------------------------------------------------------------
#
# The audit's F23 is that scam-type accuracy is 0.11-0.47 while the binary headline reads 0.85, and the
# product routes its entire playbook off the scam TYPE. The new conversation suites could not see it
# either: on 2026-07-29 the same screenshot English correctly called an SBI KYC phish was classified
# "Other Fraud" in Hinglish and answered with LOTTERY advice — and the case PASSED, because nothing
# checked what mechanism the reply named.
#
# The check is deliberately NEGATIVE. "Did the reply name the right mechanism?" is a keyword probe, and
# calibrating one earlier showed it firing on correct behaviour (a PII refusal, a reasonable clarifying
# question) — gating on that scores the regex, not the product. "Did the reply name a CONTRADICTING
# mechanism?" is different: a reply that talks about lottery processing fees when the evidence is a
# bank-KYC phish is wrong under any wording, in any language.
#
# Fires only when BOTH hold: the reply names some other mechanism, AND it does not name the expected
# one. A reply that says "this is a phishing message, not a real lottery win" is correct and must not
# trip — which is exactly why the expected-mechanism escape clause is there.
# RAIL-AMBIGUOUS mechanisms. "upi", "card" and "netbanking" are names of PAYMENT RAILS as well as of
# scam types, and a correct containment instruction says them constantly — "block the card or UPI service
# that was used" is a rail mention, not a claim about what the fraud was.
#
# Caught by this check flagging correct behaviour on BTN-step-done-advances: the bot handled a smishing
# loss well, said "UPI" only while telling the user what to block, and was scored WRONG_MECHANISM[upi].
# That is the same failure mode as the MECHANISM_NOT_NAMED probe the audit already warned about — scoring
# the regex rather than the product — and a check that cries wolf gets ignored, which costs more than
# having no check. For these three, a fraud/scam word must sit within a few words of the rail name before
# it counts as naming the mechanism.
_RAIL_AMBIGUOUS = {"upi", "card", "netbanking", "card_fraud"}

MECHANISM_TERMS = {
    "phishing": [r"phish", r"fake (link|website|sms|message)", r"\bkyc\b", r"farzi link",
                 r"नकली लिंक", r"फ़िशिंग", r"ভুয়া লিঙ্ক", r"போலி இணைப்பு"],
    "lottery": [r"lotter", r"\bprize\b", r"jackpot", r"lucky draw", r"inaam", r"लॉटरी", r"इनाम",
                r"পুরস্কার", r"லாட்டரி"],
    "job_task": [r"job offer", r"work from home", r"task (fraud|scam)", r"registration fee",
                 r"naukri", r"नौकरी"],
    "upi": [r"\bupi\b", r"\bqr\b", r"collect request", r"यूपीआई"],
    "card_fraud": [r"\bcard\b.*(fraud|charge|skim)", r"\bcvv\b", r"कार्ड फ्रॉड"],
    "sim_swap": [r"sim swap", r"sim clon", r"no service", r"सिम स्वैप"],
    "digital_arrest": [r"digital arrest", r"\bcbi\b", r"customs", r"डिजिटल अरेस्ट"],
    "ransomware": [r"ransomware", r"\bransom\b", r"encrypt", r"रैनसमवेयर"],
    "investment": [r"investment (app|scam|fraud)", r"trading (app|platform)", r"निवेश"],
    "marketplace": [r"olx", r"marketplace", r"buyer.*qr", r"seller.*qr"],
    "sextortion": [r"sextortion", r"nude", r"obscene video"],
    "remote_access": [r"anydesk", r"teamviewer", r"remote access", r"screen shar"],
}
# A MISCONCEPTION CORRECTION IS NOT A MECHANISM CLAIM.
#
# The send-vs-receive correction — "scanning a QR or entering your UPI PIN can only SEND money, never
# receive it" — has to name UPI in order to correct the belief about UPI. The mechanism check then read
# that as the reply claiming the fraud was UPI, and scored a correct marketplace answer
# WRONG_MECHANISM[upi].
#
# That is the same class of error as the appended-rails stripping below and the boilerplate stripping
# above: the grader punishing the product for saying the right thing. It matters more than usual here,
# because this exact correction is the fix for the client's OLX screenshot — so the grader was set up to
# fail precisely when the defect was repaired.
CORRECTION_SENTENCE = re.compile(
    r"[^.।\n]*(?:"
    r"(?:only|sirf|kevad|केवल|सिर्फ)[^.।\n]{0,40}(?:send|bhej|भेज)|"
    r"(?:receive|recieve|milte|मिलते)[^.।\n]{0,25}(?:nahi|not|never|नहीं)|"
    r"(?:nahi|not|never|नहीं)[^.।\n]{0,25}(?:receive|recieve|hoti|होती)|"
    r"(?:kabhi bhi|never|kabhi)[^.।\n]{0,40}(?:zaroorat nahi|not need|need not)"
    r")[^.।\n]*[.।\n]?", re.I)


def strip_corrections(text: str) -> str:
    """Remove misconception-correction sentences before the mechanism check.

    Only for that check — the text the user receives is untouched, exactly like strip_boilerplate.
    """
    return CORRECTION_SENTENCE.sub(" ", text or "")


def _mech_regex(name, terms):
    joined = "|".join(terms)
    if name not in _RAIL_AMBIGUOUS:
        return re.compile(joined, re.I)
    # Require a fraud/scam/theft word within ~40 chars either side, so a rail named in a containment
    # instruction does not read as a mechanism claim.
    ctx = r"(?:fraud|scam|scamm|phish|theft|stolen|unauthori[sz]ed|dhokha|thagi|फ्रॉड|धोखा|ठगी)"
    return re.compile(rf"(?:{ctx}.{{0,40}}(?:{joined})|(?:{joined}).{{0,40}}{ctx})", re.I | re.S)


MECHANISM_RE = {k: _mech_regex(k, v) for k, v in MECHANISM_TERMS.items()}


# Lines we appended ourselves from the action catalogue. They are procedural ("block the affected card
# or turn off UPI"), not a claim about what the fraud was — but they carry rail words next to fraud
# words, which is exactly the shape the mechanism check looks for. Counting them made a correctly
# handled smishing case score WRONG_MECHANISM[upi]. Stripped before the mechanism check only; they
# remain visible to every other check, because they ARE content the user received.
APPENDED_ACTION_LINE = re.compile(r"^\s*•.*$", re.M)


def wrong_mechanism_named(blob: str, expected: str) -> str:
    """Return the contradicting mechanism the reply named, or "" if none."""
    blob = APPENDED_ACTION_LINE.sub(" ", blob or "")
    blob = strip_corrections(blob)
    expected = (expected or "").strip().lower()
    if expected not in MECHANISM_RE:
        return ""
    if MECHANISM_RE[expected].search(blob):
        return ""                       # named the right one; anything else it mentions is context
    for name, rx in MECHANISM_RE.items():
        if name != expected and rx.search(blob):
            return name
    return ""


# ---- R6 / revalidation §3: MECHANISM_NOT_NAMED WAS ONE NUMBER FOR TWO DIFFERENT THINGS ----------
#
# It became 9 of the 10 partner_p0 failures, and the independent revalidation
# (docs/2026-08-03-response-quality-revalidation.md §3) measured it as "roughly one-third real defect
# and two-thirds grader artefact". Three of the failures it examined were replies that are correct and
# specific and merely omit a label token:
#
#   T2-09  "No genuine buyer ever needs you to scan anything or enter a PIN to pay you."
#          — the exact misconception correction this product was built to deliver. Gold wanted
#            "never receive|do not scan".
#   T2-07  "No, that is not a real HDFC Bank link." — gold wanted "not real"; the reply says
#            "not A real". One article, one failed case.
#   T2-19  the user pasted a live OTP and the PII guard REFUSED the message. Demanding a mechanism
#          name from a safety refusal is a grader bug: there was no reply in which to name anything.
#
# THE DANGER OF FIXING THIS IS LARGER THAN THE DEFECT. A gate that fails on a missing label word
# pushes the product toward emitting "This is a marketplace QR scam" INSTEAD of the sentence that
# changes the user's behaviour. So the check is split, and the halves are deliberately asymmetric:
#
#   THREAT_NOT_IDENTIFIED(F23)   HARD. The reply does not say what is happening, in any words.
#   TAXONOMY_LABEL_MISSING(F23)  SOFT. The reply says what is happening; the gold's token list does
#                                not contain the words it used.
#
# The soft half is reachable only through two NARROW doors, and the hard half is the default. It must
# stay the default: on the run this was written against, the split was 7 hard / 3 soft / 1 exempt —
# it did NOT turn a red suite green, which is the only way it is worth having. T1-03 (ransomware),
# T1-05 (insurance claim), T2-17 (card block), T2-03, T2-11, T2-13 and T1-12 all still fail HARD.
#
# WHAT THIS COSTS, stated rather than discovered later. For a LABEL-SHAPED gold the gate can no
# longer see the product stop emitting the taxonomy word, as long as it still says what is happening
# some other way. That is the recommendation's explicit intent — the label word is not the thing
# that changes a victim's behaviour — but it is a real loss of resolution, and the size of it is
# countable: 8 of partner_p0's 35 `names` golds are downgradable at all (6 label-shaped, 2
# correction-shaped) and 27 are not; across every suite it is 10 of 48. The other 79% cannot be
# softened by any reply. It is paid back by TAXONOMY_LABEL_MISSING being reported in `soft_modes` on every
# run, so the trend stays visible; if it ever climbs, that is the signal the label really did go
# away, and it is arguable in the open rather than invisible.

# Door 1: the GOLD is a taxonomy-label demand. Every alternative in `names` has to be a scam-type
# name or a legitimacy word — one procedural token ("1930", "claim", "helpline", "search") and the
# gold is asking for substance, not for a label, and a miss stays hard. That single rule is what
# keeps T2-03 (gold `1930|cybercrime|report`, reply closes a retirement-savings loss with "bas itna
# hi") on the hard side even though its reply happens to contain the word "fraud".
_LABEL_TOKENS = re.compile(
    r"^(?:"
    r"scam|scamm\w*|fraud\w*|phish\w*|fake|spoof\w*|malware|malicious app|clone\w*|deepfake|"
    r"impersonat\w*|lotter\w*|prize|jackpot|lucky draw|advance fee|processing fee|ransomware|ransom|"
    r"sim.?swap|sim clon\w*|digital arrest|sextortion|remote access|job scam|task scam|romance|"
    r"customs|investment scam|marketplace|"
    r"(?:not|isn'?t|is not)\s*\.?\*?\s*(?:a\s+)?(?:real|genuine|official|legitimate|authentic)|"
    r"real|genuine|official|legitimate|authentic|"
    # the same vocabulary in the scripts the suites actually carry
    r"स्कैम|फ़िशिंग|फिशिंग|धोखा\w*|धोखाधड़ी|ठगी|फ्रॉड|नकली|रैनसमवेयर|डिजिटल अरेस्ट|इनाम|लॉटरी|"
    r"স্ক্যাম|প্রতারণা|জালিয়াতি|ভুয়া|মোসড়ি|"
    r"மோசடி|போலி|ஏமாற்\w*|"
    r"మోసం|ఠగీ|ਠੱਗੀ|ਧੋਖਾ|फसवणूक"
    r")$", re.I)

# Door 2: the GOLD is a misconception-correction demand — a negated capability or a negated
# imperative about a payment rail, not a taxonomy word. T2-09's `never receive|scanning.*send|
# QR.*send|do not scan` is the archetype: it is not asking the reply to CLASSIFY the fraud, it is
# asking it to correct the belief that scanning a QR receives money.
_CORRECTION_TOKENS = re.compile(
    r"\b(?:never|no|not|don'?t|do not|nahi|nahin|kabhi|नहीं|कभी)\b|"
    r"\b(?:scan\w*|qr|otp|pin|password|upi|share|send|sending|receive|recieve|pay|paid)\b", re.I)


def _gold_alternatives(pattern: str) -> list:
    """The `names` gold split into its alternatives, with regex punctuation removed.

    Split on top-level `|` only — a gold like `(a|b)c` would otherwise be shredded into fragments
    that classify as neither shape. None of the current golds nest, but a future one will.
    """
    parts, depth, cur = [], 0, ""
    for ch in pattern or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "|" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    cleaned = []
    for p in parts:
        p = re.sub(r"\\b|\(\?:|[()\[\]{}^$+?]", "", p).strip()
        p = re.sub(r"\s+", " ", p)
        if p:
            cleaned.append(p)
    return cleaned


def gold_shape(pattern: str) -> str:
    """"label" | "correction" | "substantive".

    ALL alternatives must agree, and "substantive" is the default. That asymmetry is the safety
    property: a gold has to be UNANIMOUSLY a label demand before a miss can be downgraded, so one
    substantive token in a mixed gold (`fake|official|not.*genuine|search` — T2-13) keeps the whole
    case hard. `.` (the "any reply at all" gold four cases carry) is substantive by this rule, which
    is correct: it can only fail when there was no reply.
    """
    alts = _gold_alternatives(pattern)
    if not alts:
        return "substantive"
    if all(_LABEL_TOKENS.match(a) for a in alts):
        return "label"
    if all(_CORRECTION_TOKENS.search(a) for a in alts):
        return "correction"
    return "substantive"


# "the national cyber-fraud helpline" is the PROPER NAME OF 1930, not a statement about this user's
# case. It rides along in the action catalogue's own copy, so without this every reply that tells a
# victim to call 1930 would read as having identified the threat — which is precisely the "scoring
# the regex, not the product" failure the mechanism check above was rewritten to avoid, pointed the
# other way. Stripped for the identification test only; the user still receives it.
HELPLINE_PROPER_NOUN = re.compile(
    r"(?:national\s+)?cyber[\s.-]?(?:crime|fraud)\s+(?:helpline|portal)|"
    r"national\s+cybercrime\s+helpline|cybercrime\.?\s?gov\.?\s?in", re.I)

# THREAT IDENTIFIED — two families, both requiring the reply to make a CLAIM about what is going on.
#   (a) a fraud/scam word in any script the suites carry;
#   (b) a verdict on legitimacy: "that is not a real HDFC link", "no bank ever sends", "farzi".
# Deliberately NOT included: 1930, cybercrime.gov.in, "block the card", "do not pay". Those are
# actions. T1-03's reply contains all of them and still never tells the user their laptop is holding
# their business data to ransom, and it must keep failing.
THREAT_IDENTIFIED = re.compile(
    r"\b(?:scam|scams|scammer\w*|scamming|fraud|frauds|fraudster\w*|fraudulent|phish\w*|"
    r"malware|malicious|ransomware|sextortion|deepfake|spoof\w*|impersonat\w*|"
    r"thag\w*|thug\w*|dhokha|dhoka|farzi|farzee|nakli|jaali|"
    r"धोखा\w*|धोखाधड़ी|ठगी|फ्रॉड|स्कैम|फ़िशिंग|फिशिंग|नकली|फर्जी|जालसाज\w*|स्पूफ|"
    r"প্রতারণা|জালিয়াতি|স্ক্যাম|ভুয়া|প্রতারক|"
    r"மோசடி|போலி|ஏமாற்\w*|"
    r"మోసం|ਠੱਗੀ|ਧੋਖਾ|फसवणूक)\b|"
    # LEGITIMACY VERDICTS. The gap between the first draft of this and live traffic is instructive:
    # the draft matched "that is not a real HDFC Bank link" and MISSED "that link does not look
    # like an official HDFC Bank link" — the same verdict, hedged, and the exact wording the
    # revalidation quotes for T2-07. Both product phrasings were observed on 2026-08-04, four hours
    # apart, on an unchanged case. So the negation is allowed up to three words of distance from the
    # legitimacy adjective rather than having to sit against it.
    r"(?:is|are|was|were|that'?s|it'?s)\s+not\s+(?:a\s+|an\s+|the\s+)?(?:real|genuine|official|"
    r"legitimate|authentic|valid)|"
    r"(?:\bnot|n'?t)\s+(?:\w+\s+){0,3}(?:a\s+|an\s+|the\s+)?(?:real|genuine|official|legitimate|"
    r"authentic)\b|"
    r"(?:asli|sahi|official|genuine)\s+nahi\s+(?:lag|hai|h\b)|"
    r"\bno\s+(?:genuine|real|legitimate|official)\s+\w+\s+(?:ever\s+)?(?:needs?|asks?|requires?|"
    r"will|would|sends?)|"
    r"\bno\s+bank\s+(?:ever\s+)?(?:asks?|sends?|will|would|calls?)|"
    r"\bbanks?\s+(?:do|does)\s+not\s+(?:send|ask|suspend|call|threaten)|"
    r"koi\s+bank\s+kabhi|asli\s+nahi|"
    r"कोई\s+(?:भी\s+)?बैंक\s+[^।\n]{0,40}(?:कभी|नहीं)|असली\s+नहीं",
    re.I)

# The misconception correction, in the forms the product actually emits. CORRECTION_SENTENCE above
# is tuned for STRIPPING (it must not over-reach into ordinary prose); this is tuned for DETECTING,
# and adds the "no genuine buyer ever needs you to scan" shape that T2-09 delivers.
MISCONCEPTION_CORRECTED = re.compile(
    r"\bno\s+(?:genuine|real|legitimate)\s+\w+\s+(?:ever\s+)?(?:needs?|asks?|requires?)|"
    r"(?:scan\w*|qr|pin)[^.।\n]{0,60}(?:only|never|cannot|can'?t|nahi|नहीं|केवल)[^.।\n]{0,30}"
    r"(?:send|receive|recieve|bhej|मिल|भेज)|"
    r"(?:only|sirf|केवल|सिर्फ)[^.।\n]{0,40}(?:send|bhej|भेज)[^.।\n]{0,30}(?:never|not|nahi|नहीं)|"
    r"(?:never|do not|don'?t|kabhi|कभी)\s+(?:share|give|batayein|बताएँ|बताएं)[^.।\n]{0,40}"
    r"(?:otp|pin|password|पासवर्ड)", re.I)


def _pii_refusals() -> list:
    """The PII guard's refusal copy, en/hi/hing, read from the product's own source.

    Same discipline as `_live_disclaimers`: the exemption is keyed to the string the product actually
    emits (internal/handlers/user_messages.go, msgPIIBlocked), so it cannot be widened by a
    paraphrase and it goes NARROW — never broad — if the copy moves.
    """
    # Follows the same move as _live_disclaimers above, for the same reason and after the same
    # failure: the Go tree was deleted in the Python port, so this parsed nothing and the exemption
    # switched itself off. A refused turn then scored THREAT_NOT_IDENTIFIED, which convicts the
    # product of missing a threat on a turn where it correctly REFUSED to process the message. That
    # is the worst direction for this particular check to be wrong in, because it penalises the safety
    # behaviour. Product owns the string in the Python catalogue now.
    out = []
    try:
        with open(_PY_CATALOGUE, encoding="utf-8") as fh:
            py_src = fh.read()
    except OSError:
        py_src = ""
    m = re.search(r'"pii_blocked"\s*:\s*\{(.*?)\n\s*\},', py_src, re.S)
    if m:
        out += [s for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if len(s) > 40]

    try:
        with open(_USER_MESSAGES_GO, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        src = ""
    m = re.search(r"func msgPIIBlocked\(lang string\) string \{(.*?)\n\}", src, re.S)
    if m:
        out += [s for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if len(s) > 40]
    return out


PII_REFUSALS = _pii_refusals()
if not PII_REFUSALS:
    print("WARNING (R6): could not parse msgPIIBlocked from user_messages.go — the safety-refusal "
          "exemption is DISABLED, so a refused turn will score THREAT_NOT_IDENTIFIED", file=sys.stderr)


def safety_refusal_suppressed(replies) -> bool:
    """True only when the safety guard left the bot with nothing else to say.

    THE EXEMPTION CANNOT LAUNDER A GENUINE MISS, and that is the whole design. It requires that after
    removing the refusal itself and the standing disclaimer there is NO substantive bot content left
    in the entire conversation — i.e. there existed no reply in which a mechanism could have been
    named. A refusal followed (or preceded) by a real answer that fails to identify the threat is not
    exempt: the bot had its turn and did not use it. Appended action bullets are deliberately NOT
    stripped here, so even a refusal-plus-checklist case stays gradeable.
    """
    if not PII_REFUSALS:
        return False
    if not any(any(r in rep for r in PII_REFUSALS) for rep in replies):
        return False
    residue = " ".join(rep for rep in replies if not any(r in rep for r in PII_REFUSALS))
    return len(strip_boilerplate(residue).strip()) < 40


def classify_names_miss(blob: str, names_gold: str, replies) -> str:
    """Why the `names` gold did not match: "exempt" | "cosmetic" | "real".

    `blob` is already boilerplate-stripped and digit-folded by score(); appended action lines and
    the 1930 proper noun come off here, for the same reason they come off before the mechanism check.
    """
    if safety_refusal_suppressed(replies):
        return "exempt"
    text = HELPLINE_PROPER_NOUN.sub(" ", APPENDED_ACTION_LINE.sub(" ", blob or ""))
    shape = gold_shape(names_gold)
    if shape == "label" and THREAT_IDENTIFIED.search(text):
        return "cosmetic"
    if shape == "correction" and (MISCONCEPTION_CORRECTED.search(text)
                                  or THREAT_IDENTIFIED.search(text)):
        return "cosmetic"
    return "real"


# ---- QS-07: THE CONTENT GRADERS WERE ENGLISH + DEVANAGARI ONLY ---------------------------------
#
# Eight of twelve content graders below matched English, Hinglish and Devanagari and nothing else, so
# every P0 CLASS they gate — a confirmation cue on a prohibition, a duplicated handoff ack, asking the
# user to repeat themselves, premature closure, self-contradiction — was UNMEASURABLE in Tamil,
# Bangla, Telugu, Marathi and Punjabi. Those graders returned "no violation found" in exactly the
# languages the audit says are structurally broken, and a clean result from a grader that cannot read
# the text is the most expensive kind of green there is. It is the same measurement bias that once
# scored correct Tamil urgency replies as failures, pointed the other way.
#
# Terms are high-signal and deliberately few. The job of each of these is to detect that a KNOWN
# defect shape occurred, not to parse the language; a short list of unambiguous stems catches the
# defect while a long one starts matching ordinary prose. Marathi shares Devanagari with Hindi and is
# covered by the Devanagari alternatives already present, with the same caveat SCRIPTS records: it
# proves the reply is not English, not that it is Marathi.
#
# HONESTY NOTE, which belongs next to the code and not only in a report: extending a grader to a
# language does not create data for it. hi/bn evaluation content is 100% gemini-synthetic, so a bn
# number produced by these graders measures generator self-consistency, not the product's measured
# performance for a Bangla speaker. What this change buys is that the defect is now VISIBLE when it
# occurs, which is a precondition for the number meaning anything later — not the number itself.

# The confirmation cue, in every form the two emitters produce.
CONFIRM_CUE = re.compile(
    r"tell me once that'?s done|we'?ll move on|bata dijiye|bata dena|aage badhte hain|"
    r"बता दीजिए|आगे बढ़ते हैं|reply DONE|DONE likhein|just say so and we'?ll carry on|"
    r"হয়ে গেলে জানান|জানাবেন|করে জানান|"                                    # bn
    r"முடிந்ததும் சொல்லுங்கள்|சொல்லுங்கள்|தெரிவிக்கவும்|"                      # ta
    r"అయిన తర్వాత చెప్పండి|చెప్పండి|తెలియజేయండి|"                              # te
    r"ਹੋ ਜਾਣ ਤੇ ਦੱਸੋ|ਦੱਸ ਦਿਓ|ਦੱਸੋ ਜੀ|"                                        # pa
    r"झाल्यावर सांगा|कळवा", re.I)                                             # mr
HANDOFF_ACK = re.compile(
    r"connecting you to a member of our team|received your request to speak with our team|"
    r"team will call you back|connect kar rahi hoon|"
    # GRADER BUG, FIXED 2026-08-15, same family as the one in HANDOFF above. This pattern covers
    # bn/ta/te/pa/mr and skipped HINDI, the language the product authors its handoff ack in:
    # `turn._HANDOFF_ACK["hi"]` matched nothing here, so DUPLICATE_HANDOFF_ACK - the check that a
    # handoff is acknowledged once and not twice - was unmeasurable in Hindi and Hinglish. Both are
    # read off the product's authored strings; `test_grader_handoff_containment` re-derives them.
    r"हमारी टीम[^।\n]{0,40}(कॉल|संपर्क|जोड़)|टीम के एक सदस्य|टीम का एक सदस्य|"
    r"team (ka|ke) ek member|priority par hamari team|"
    r"আমাদের টিম[^।\n]{0,30}(কল|যোগাযোগ)|টিমের সাথে যুক্ত|"                   # bn
    r"எங்கள் குழு[^.\n]{0,30}(அழைப்|தொடர்பு)|குழுவுடன் இணை|"                   # ta
    r"మా బృందం[^.\n]{0,30}(కాల్|సంప్రదిస్)|బృందానికి కనెక్ట్|"                  # te
    r"ਸਾਡੀ ਟੀਮ[^।\n]{0,30}(ਕਾਲ|ਸੰਪਰਕ)|ਟੀਮ ਨਾਲ ਜੋੜ|"                          # pa
    r"आमची टीम[^।\n]{0,30}(कॉल|संपर्क)", re.I)                                # mr
# The offline / independent routes a user can take when 1930 and the portal are both unreachable.
# Deliberately broad across languages and phrasings: the point is that SOMETHING actionable that does
# not depend on the failed channel was offered, not that a particular wording appeared.
ALTERNATIVE_ROUTE = re.compile(
    r"police station|cyber cell|cyber crime cell|\bfir\b|in person|physically|"
    r"nearest police|local police|written complaint|bank branch|visit (your|the) branch|"
    r"पुलिस स्टेशन|साइबर सेल|एफ\.?आई\.?आर|थाने|पोलीस ठाणे|सायबर सेल|शाखेत|"      # hi + mr
    r"police station jaa|thane|cyber cell jaa|khud jaakar|"
    r"থানা|পুলিশ স্টেশন|সাইবার সেল|শাখায়|সশরীরে|লিখিত অভিযোগ|"                # bn
    r"காவல் நிலைய|சைபர் செல்|கிளைக்கு|நேரில்|எழுத்துப்பூர்வ புகார்|"              # ta
    r"పోలీస్ స్టేషన్|సైబర్ సెల్|శాఖకు|నేరుగా వెళ్ళి|లిఖిత ఫిర్యాదు|"               # te
    r"ਥਾਣਾ|ਪੁਲਿਸ ਸਟੇਸ਼ਨ|ਸਾਈਬਰ ਸੈੱਲ|ਸ਼ਾਖਾ|ਲਿਖਤੀ ਸ਼ਿਕਾਇਤ", re.I)                  # pa

ASK_TO_REPEAT = re.compile(
    r"tell me (a bit )?more about what|could you (please )?(tell|explain|describe)|"
    r"what (exactly )?happened\?|can you (tell|explain) me (again|more)|"
    r"phir se batayein|dobara batayein|kya hua tha|thoda aur batayein|"
    r"फिर से बताइए|दोबारा बताएं|क्या हुआ था|पुन्हा सांगा|काय झाले|"                # hi + mr
    r"কী হয়েছে|আবার বলুন|আরেকটু বলুন|খুলে বলুন|"                              # bn
    r"என்ன நடந்தது|மீண்டும் சொல்|இன்னும் கொஞ்சம் சொல்|விளக்கமாக சொல்|"           # ta
    r"ఏమి జరిగింది|మళ్ళీ చెప్ప|ఇంకా కొంచెం చెప్ప|వివరంగా చెప్ప|"                  # te
    r"ਕੀ ਹੋਇਆ|ਦੁਬਾਰਾ ਦੱਸੋ|ਥੋੜਾ ਹੋਰ ਦੱਸੋ", re.I)                                 # pa
# ---- self-contradiction: "nothing more to do", then an obligation ------------------------------
#
# The client's complaint, twice, in their own screenshots: bot "Bas abhi ke liye itna hi kaafi hai" →
# user "bank ko call karne ki zaroorat hai kya?" → bot "Haan, bank ko call karna zaroori hai".
# (v2/image copy 11.png and image copy 13.png; replayed by V2X-12 and V2X-13.)
#
# Three things were wrong with the check that used to live here and they compounded:
#
#  1. IT WAS SOFT, so it could not fail a build. The client's most-repeated complaint was therefore
#     unenforceable, and V2X-12/13 both carry a note saying so and gate on the root cause instead.
#     It is HARD now: saying "you're done" and then "you must" is wrong under any wording, in any
#     language, which is precisely the bar the module docstring sets for a hard check.
#  2. MUST_DO's Hinglish arm was `haan.{0,40}(zaroori|karna)`, which requires the *infinitive*. The
#     clearest instance in the corpus — "Haan, apne telecom operator ko bhi inform kar do" — uses the
#     imperative `kar do` and was missed. A check that cannot see the example it was written for is
#     not a check. Imperatives (kar do / kar lo / kar lijiye / karein / karo), obligation words
#     (chahiye, zaroori hai) and the Devanagari forms are all matched now.
#  3. IT WAS ORDER-FREE, over the whole conversation blob. That is what forced the narrow regex:
#     "Haan, bilkul sahi kaha. Abhi ke liye bas itna hi karna tha." — one sentence of agreement plus
#     closure — contains both halves and is entirely correct. Broadening MUST_DO without fixing the
#     ordering would have converted a precision problem into a flood of false positives on good
#     replies. The contradiction is temporal by definition, so it is now measured that way: a turn
#     that closes the case, then a LATER turn that reopens it. Same-turn co-occurrence does not fire.
#
# Calibrated over 2,027 archived conversations in eval/reports/: the ordered rule fires 0 times, i.e.
# it adds no false positives to anything already frozen green. It is proved capable of failing
# against a known-bad fixture in services/intelligence/tests/test_contradiction_gate.py — a gate
# nobody has watched fail is a gate nobody knows works.
NOTHING_MORE = re.compile(
    r"nothing (more|else) (to do|is needed)|no further action|that'?s all for now|"
    r"you (don'?t|do not) need to do anything (more|else)|"
    r"bas itna hi|itna hi kaafi|bas yahi (main )?steps? the|"
    r"(aur )?kuch (aur )?kar(ne|na) ki (zaroorat|zarurat) nahi|"
    r"बस इतना ही|इतना ही काफ़ी|कुछ और करने की (ज़रूरत|जरूरत) नहीं|"
    r"आणखी काही करण्याची गरज नाही|एवढेच पुरे|"                                 # mr (QS-07)
    r"আর কিছু করার নেই|এটুকুই যথেষ্ট|আর কিছু দরকার নেই|"                       # bn
    r"வேறு ஒன்றும் செய்ய (வேண்டாம்|தேவையில்லை)|இவ்வளவு போதும்|"                # ta
    r"ఇంకేమీ చేయ(నవసరం లేదు|వలసిన పని లేదు)|ఇంతే చాలు|"                        # te
    r"ਹੋਰ ਕੁਝ ਕਰਨ ਦੀ ਲੋੜ ਨਹੀਂ|ਬਸ ਇੰਨਾ ਹੀ", re.I)                              # pa

# AGREEMENT IS NOT AN OBLIGATION. "Haan, bilkul sahi kaha" is the bot confirming the user's own
# summary, and it is the single most common way a Hinglish reply opens. Stripped before MUST_DO for
# the same reason strip_boilerplate and strip_corrections exist: the grader must not punish the
# product for saying the right thing.
# THE LADDER'S OWN DECLARED SIZE — wikiStepPlanPreamble, internal/handlers/wiki_fallback.go:1250,
# in the three languages it is authored in. The bot states how much is owed; a grader should read that
# rather than guess it from formatting.
DECLARED_STEP_COUNT = re.compile(
    r"there are (\d+) things to do|"
    r"कुल (\d+) काम करने हैं|"
    r"kul (\d+) kaam karne hain", re.I)

# CLAIMING THE CASE IS DONE, in the forms the product actually emits. Separate from NOTHING_MORE
# because that regex was built for the "nothing more is needed" family and this defect closes with the
# stronger and more misleading claim that the USER has already done it all — measured live as "Not for
# now, you've done everything needed at this point."
CLOSURE_CLAIM = re.compile(
    r"you'?ve done everything|you have done everything|done everything needed|"
    r"that'?s everything (for now|you need)|not for now[,.]? you|"
    r"aapka kaam poora ho gaya|sab kuch ho gaya hai|aapne sab kuch kar diya|"
    r"आपका काम पूरा हो गया|सब कुछ हो गया", re.I)

# THE USER SAYING THEY DID IT. Deliberately narrow: an acknowledgement ("ok", "theek hai") is not a
# completion report, and treating it as one is the same conflation that made "ok" imply low literacy.
# Only an explicit "I have done / it is done" counts.
USER_DONE_CUE = re.compile(
    r"\b(i )?(have )?(done|did) (it|that|them|this)\b|\bdone (it|that|them)\b|"
    r"\bi (called|blocked|filed|reported|changed)\b|\ball done\b|\bcompleted\b|"
    r"kar (diya|liya)|ho gaya|ho gaye|kar diye|call kiya|block kar diya|"
    r"कर दिया|हो गया|हो गए|"
    r"করে ফেলেছি|হয়ে গেছে|"
    r"செய்துவிட்டேன்|முடிந்தது|"
    r"చేశాను|అయిపోయింది|"
    r"ਕਰ ਦਿੱਤਾ|ਹੋ ਗਿਆ", re.I)

AGREEMENT_ONLY = re.compile(
    r"\b(haan|haa|ji haan|yes|yep|bilkul)\b[,! ]*"
    r"(bilkul\s+)?(sahi|theek|thik|right|correct)[^.।\n]*[.।\n]?", re.I)

# ---- R3 §2.1: the two architectures, and which one the victim got ------------------------------
#
# Round 3's finding, and it is larger than the ten items it was checking: THERE ARE TWO RESPONSE
# ARCHITECTURES FOR THE SAME INCIDENT, and which one a victim gets depends on the words they open
# with. Eight confirmed-loss openers, each followed by "ok done" then "anything else i need to do?":
# four never reached the authored plan at all, and five told the victim "that covers everything".
#
# On the ledger path everything works — confirmation crediting, the prohibition partition, the
# truthful closing head. Off it, the reply is free text that cannot say what is left, does not know
# what was done, and asserts completeness when challenged. So the fence is not about phrasing; it is
# about WHICH ARCHITECTURE ANSWERED, and that is legible from the transcript alone because both
# architectures have authored, greppable heads.
#
# PLAN_MARKER is the union of the authored plan's own copy, taken from the source rather than from a
# transcript so the fence and the product cannot drift:
#   _CHECKLIST_HEAD       services/intelligence/app/actions.py:2018  "Here is everything that needs
#                                                                     doing — {n} things in all"
#   _WL_HEAD_ASKS         actions.py:1324  "Yes — these are still outstanding:"
#   _WL_HEAD_CLOSING_*    actions.py:1329  "Before you go — ... still open:"
#   _WL_HEAD_REST         actions.py:1340  "After that, still to come"
#   _WL_HEAD_RULES        actions.py:1347  "These aren't tasks — just keep to them throughout:"
#   wikiStepPlanPreamble  internal/handlers/wiki_fallback.go:1250  "There are N things to do"
# All three authored languages (en/hi/hing), because a fence that only reads English would score the
# Hinglish opener — one of the three that WORKS today — as a miss and invert its own signal.
PLAN_MARKER = re.compile(
    r"here is everything that needs doing|these are still outstanding|"
    r"before you go[ ,—-]*(these are|one thing)|after that,? still to come|"
    r"these aren'?t tasks|there are \d+ things to do|"
    r"ये सब करना है|ये अभी बाकी ह|जाने से पहले|उसके बाद ये बाकी रहेंगे|ये काम नहीं हैं|कुल \d+ काम|"
    r"yeh sab karna hai|yeh abhi baaki hain|jaane se pehle|uske baad yeh baaki rahenge|"
    r"yeh kaam nahi hain|kul \d+ kaam", re.I)

# CLAIMING THE JOB IS COMPLETE, in the forms round 3 measured on the free-text path.
#
# Deliberately a THIRD regex rather than a widening of NOTHING_MORE or CLOSURE_CLAIM, and that is a
# finding in itself: round 3's worst verbatim — "No, that covers everything that needs doing right
# now." — matches NEITHER of the two regexes already in this file. CLOSURE_CLAIM has
# `that'?s everything (for now|you need)`, which does not cover "that COVERS everything". So the
# existing CLOSE-01 check could not have seen the sentence round 3 quotes as the worst instance.
# Widening a shared regex while five streams are landing product changes would make every flip
# unattributable, so the gap is recorded here and the new fence reads its own union. Whoever owns
# CLOSE-01 next should fold this in; see the report for A6, round 3.
COMPLETENESS_CLAIM = re.compile(
    r"that covers everything|covers everything that needs doing|"
    r"you'?ve already done everything|you have already done everything|"
    r"yes,? i'?m sure\b|nothing (?:further|more) is (?:needed|required)|"
    r"abhi ke liye itna hi zaroori tha|is case mein jo zaroori tha,? wo ho chuka|"
    r"अभी के लिए इतना ही ज़रूरी था|इस मामले में जो ज़रूरी था", re.I)

# ASKING A CONFIRMED-LOSS VICTIM TO START OVER. Round 3's OTP-vishing variant, challenged directly,
# lost the conversation entirely: "I'm here to help you with any fraud concerns. Could you please tell
# me a little more about what happened?" — after the user had already stated the loss and the amount.
# Scoped to the LAST turn by its only caller, because an opening clarification is legitimate.
# THE PII GATE'S REFUSAL, in the three languages it is authored in (msgPIIBlocked,
# internal/handlers/user_messages.go:203). Matched so that "the plan was never reached" can say
# whether the turn was ROUTED WRONG or REFUSED OUTRIGHT — see the call site.
PII_REFUSAL = re.compile(
    r"cannot process messages containing sensitive|"
    r"संवेदनशील व्यक्तिगत या वित्तीय जानकारी वाले संदेश|"
    r"sensitive personal ya financial info wale messages", re.I)

LOST_THE_THREAD = re.compile(
    r"here to help you with any fraud concerns|tell me (?:a little |a bit )?more about what happened|"
    r"could you (?:please )?(?:tell|explain|describe) me (?:more|again)|"
    r"kya hua tha|thoda aur batayein|phir se batayein|"
    r"क्या हुआ था|थोड़ा और बताइए|फिर से बताइए", re.I)


# ---- R3 §2.5: DETERMINISM, MEASURED ON THE CLASS AND NEVER ON THE STRING -----------------------
#
# Round 3 ran ONE conversation three times, unchanged, and got three different answers to the same
# "anything else i need to do?": a discovery prompt, a false all-clear, and a clarification. Those are
# not three phrasings of one answer, they are three different products, and one of them is the harm.
#
# A model writes the sentence, so the sentence will differ every run and asserting on it would be a
# flake generator — the exact thing §3 is about. What must not differ is WHICH OF FOUR THINGS the turn
# did. The classes are ordered by how wrong they are, and the first match wins, because a reply that
# both names outstanding work and asks a question has named outstanding work:
#
#   outstanding — named at least one thing still to do            (the correct answer)
#   complete    — claimed the job is finished                     (the harm)
#   question    — asked the user something instead of answering   (round 3's discovery prompt)
#   other       — none of the above
# THE USER SAYING THEY DID IT, INCLUDING THE BARE FORM. USER_DONE_CUE above deliberately requires an
# object ("done it", "kar diya") because it is used to decide whether a ladder was worked through, and
# reading a bare "ok" as completion is the conflation that once made "ok" imply low literacy. But the
# openers in this fence answer the plan with exactly "ok done", and a check that scored that as ZERO
# confirmations would pin the wrong contract: it would fail a future product that correctly credits
# three "ok done"s and then truthfully says the case is closed. So this one counts the bare form too,
# and only this one. Under-counting a confirmation makes a completeness claim look more false than it
# is, which is the direction a fence must never lean.
USER_CONFIRMS_DONE = re.compile(
    r"\b(?:ok(?:ay)?[, ]+)?done\b|\bfinished\b|\bcompleted\b|"
    r"\bkar (?:diya|liya|di|diye)\b|\bho gaya\b|\bho gaye\b|\bhogaya\b|"
    r"कर दिया|हो गया|हो गए|"
    r"করে ফেলেছি|হয়ে গেছে|"
    r"செய்துவிட்டேன்|முடிந்தது|"
    r"చేశాను|అయిపోయింది|"
    r"ਕਰ ਦਿੱਤਾ|ਹੋ ਗਿਆ", re.I)


def rail_pattern(rail: str) -> re.Pattern:
    """A rail as a literal, but tolerant of whitespace injected around punctuation.

    Not pedantry. Round 1 shipped `cybercrime. gov. in` — the statutory portal, unclickable — to every
    user told to report, because a grounding fence rebuilt approved text with `join(kept, " ")`. A
    strict substring check would score that reply as "the rail was never given", which is the wrong
    complaint about the right sentence: the rail WAS given and then broken. This check is about
    omission; the broken-URL defect has its own fence and should not be double-counted here.
    """
    return re.compile(r"\s*".join(re.escape(ch) for ch in str(rail).strip()), re.I)


def closing_class(text: str) -> str:
    """The CLASS of an "anything else?" answer. Deterministic, no LLM, no network."""
    t = str(text or "")
    if not t.strip():
        return "empty"
    if PLAN_MARKER.search(t):
        return "outstanding"
    if COMPLETENESS_CLAIM.search(t) or CLOSURE_CLAIM.search(t) or NOTHING_MORE.search(t):
        return "complete"
    if LOST_THE_THREAD.search(t) or ASK_TO_REPEAT.search(t) or "?" in t:
        return "question"
    return "other"

# QS-07: the affirmation and the obligation both have to be readable, or SELF_CONTRADICTION — the
# client's most-repeated complaint — is undetectable outside English/Hindi. `[^.।\n]` is widened to
# also stop at the Bangla/Devanagari danda and the Tamil/Telugu full stop, so the "affirm then oblige"
# window does not run across sentence boundaries in those scripts and over-match.
_AFFIRM = (r"(?:\b(?:yes|yeah|haan|haa|ji haan)\b|हाँ|हां|जी हाँ|होय|"
           r"হ্যাঁ|হ্যা|আম|ஆம்|ஆமாம்|అవును|ਹਾਂ|ਜੀ ਹਾਂ)")
MUST_DO = re.compile(
    _AFFIRM + r"[^.।\n]{0,70}?"
    r"(?:you (?:should|must|need|have to)|it'?s (?:important|essential|critical) to|"
    r"please (?:call|inform|report|contact|block)|"
    r"(?:zaroori|zarurat) hai|chahiye|"
    r"(?:inform|call|report|contact|block|band|change|file)\s+kar\b|"
    r"kar\s*(?:do|lo|len|lena|lijiye|dijiye|dena|deni)\b|"
    r"kar(?:ein|iye|na|o)\b|"
    r"(?:करना|करें|कीजिए|कीजिये|ज़रूरी|जरूरी|चाहिए|करा|करावे|आवश्यक)|"           # hi + mr
    r"(?:করুন|করতে হবে|করা দরকার|জরুরি)|"                                     # bn
    r"(?:செய்யுங்கள்|செய்ய வேண்டும்|அவசியம்|கட்டாயம்)|"                          # ta
    r"(?:చేయండి|చేయాలి|అవసరం|తప్పనిసరి)|"                                      # te
    r"(?:ਕਰੋ|ਕਰਨਾ (?:ਪਵੇਗਾ|ਚਾਹੀਦਾ)|ਜ਼ਰੂਰੀ))", re.I)                             # pa


def contradiction_turns(res):
    """(closing_turn, reopening_turn) 0-based, or None.

    Bot replies only, in order. A conversation contradicts itself when one turn says the user is
    done and a STRICTLY LATER turn tells them to act. Returns the pair so the fail label can name
    the turns, which is what makes the label attributable to a turn row in DuckDB.
    """
    per = ["\n".join(t.get("replies") or []) for t in res.get("turns") or []]
    closed = None
    for i, text in enumerate(per):
        if closed is None:
            if NOTHING_MORE.search(text):
                closed = i
            continue
        if MUST_DO.search(AGREEMENT_ONLY.sub(" ", text)):
            return closed, i
    return None


class HarnessError(Exception):
    """Infrastructure failed, the product did not. Must never be scored as a product defect."""


class RateLimited(HarnessError):
    """A 429 survived every retry. Reported as its own mode: a throttle is not a verdict."""


# How many 429s this process absorbed, and how much wall-clock it spent doing it. Counted rather than
# only logged, because "the gate was slow and green" and "the gate was fast and green" are different
# facts about the upstream and only one of them is worth acting on.
THROTTLE = {"http_429": 0, "backoff_s": 0.0, "exhausted": 0}


def _post(path, payload, attempts=6):
    """POST with backoff on transient failures, and a LONGER, separate curve for 429.

    A rate-limit or a 5xx is the HARNESS being throttled, not the bot failing to answer. Scoring it as
    a dropped turn is how infrastructure noise becomes a fake regression: one run came back 22/36 with
    11 "no reply" purely because the test API returned 429, while the very next run scored 34/36 on the
    same build. An eval that cannot tell those apart is not measuring the product (audit F25d, inverted:
    a stall must not read as a pass, AND a throttle must not read as a failure).

    WHY 429 GETS ITS OWN CURVE (round 3 §3). The old curve was 2/3/5/9s for every transient code alike,
    which is right for a connection reset and far too short for a shed request: the Test API sheds at
    testAPITurnAcquireTimeout = 5s while a turn takes up to 11s (measured, MINED-1983259418 turn 2), so
    a caller retrying at 2s is simply shed again, three times, and then gives up on infrastructure that
    was working. 429 now waits out a whole turn: 8/16/24/32/40/48s, capped, six attempts.
    """
    last = None
    for i in range(attempts):
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": KEY}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r).get("data") or {}
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                THROTTLE["http_429"] += 1
                delay = min(8 * (i + 1), 48)     # 8, 16, 24, 32, 40, 48
                THROTTLE["backoff_s"] += delay
                print(f"PACING: 429 on {path} (attempt {i+1}/{attempts}); backing off {delay}s",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            if e.code in (500, 502, 503, 504):
                time.sleep(2 ** i + 1)          # 2s, 3s, 5s, 9s, 17s, 33s
                continue
            raise
        except Exception as e:                   # connection reset / timeout
            last = e
            time.sleep(2 ** i + 1)
    if isinstance(last, urllib.error.HTTPError) and last.code == 429:
        THROTTLE["exhausted"] += 1
        raise RateLimited(f"{path}: HTTP 429 after {attempts} attempts")
    raise HarnessError(f"{path}: {last}")


# ---- IS THE SERVER ON THE OTHER END EVEN OUR PRODUCT? (2026-08-05) -----------------------------
#
# boxxv2.shunyalabs.ai proxies to 127.0.0.1:8100, which is `boxx_app`, the Go-era system. This branch
# is `boxxpy_app` on 127.0.0.1:8200 and is served at boxxv3.shunyalabs.ai. Both hosts expose the same
# Test API, accept the same case ids and return the same report shape, so a whole sweep aimed at the
# wrong one produces a scoreboard that looks exactly like a measurement of this build and is a
# measurement of a different product. That happened, repeatedly, and every score taken that way had to
# be thrown out.
#
# The tell is one sentence: the old app opens with a robot pictograph and a bold "boxx Insurance"
# heading; this build opens with the product's own authored notice. So the harness spends ONE turn
# proving which product answered before it scores anything.
#
# THE EXPECTED PREFIX IS READ OUT OF THE CATALOGUE, NEVER TRANSCRIBED. A hand-copied string is the
# defect `_live_disclaimers()` above has now recorded four times, always failing silent and always in
# the direction of a false pass. Reading `default_disclaimer_part_1`'s en value means a copy change
# moves this check in the same commit.
#
# THERE IS NO LONGER A HARDCODED FALLBACK, and `authored_opener_prefix` records what the fallback
# cost: it held "Quick note:" from a version of the disclaimer that had been reworded away, the
# extraction quietly failed over to it, and every conversation eval against boxxv3 refused to run
# against a healthy build. A hardcoded copy of product text is exactly as stale-prone in the
# fallback as it is in the check.

#: What the target proved about itself, filled by `assert_target_is_our_build` and stamped into
#: every report header. A report that cannot say which engine and which model produced it is not a
#: measurement of anything, and thirteen of the fifteen frozen greensets in this repo are baselines
#: of the OLD product for exactly that reason.
TARGET: dict = {"engine": "", "ai": {}}


def _refuse_target(want, opener, why, extra):
    """Print why the target is unscoreable and exit 2. Never returns.

    Exit 2 is `gate()`'s "this run cannot certify anything", not 1: a refusal is not a red suite,
    and a CI that treats them the same will eventually "fix" a refusal by lowering a threshold.
    """
    print("=" * 100, file=sys.stderr)
    print(f"REFUSING TO RUN. {BASE} is not serving a scoreable build.", file=sys.stderr)
    if extra:
        print(f"  {extra}", file=sys.stderr)
    print(f"  expected the opener to begin {want!r}, from backend/app/core/messages.py",
          file=sys.stderr)
    print(f"  got: {(opener or '(no reply: ' + why + ')')[:200]!r}", file=sys.stderr)
    print("  boxxv2.shunyalabs.ai and 127.0.0.1:8100 are boxx_app, the OLD product. This branch is",
          file=sys.stderr)
    print("  boxxpy_app on 127.0.0.1:8200, served at https://boxxv3.shunyalabs.ai.", file=sys.stderr)
    print("  Point --base (or BOXX_TEST_BASE) at that host and run again.", file=sys.stderr)
    print("=" * 100, file=sys.stderr)
    sys.exit(2)


def authored_opener_prefix() -> str:
    """The opening SENTENCE of the English notice, read out of the catalogue.

    What identifies the build is which product wrote the notice. A leading sentence survives a
    reword of everything after it while still separating this app from the old one, whose notice
    begins with a pictograph and a bold heading.

    THIS SILENTLY RETURNED STALE COPY AND REFUSED EVERY RUN. The line was

        head = (en.group(1).split(":")[0].strip() + ":") if en and ":" in en.group(1) else ""

    which required the authored English disclaimer to contain a COLON, because it once read
    "Quick note: I'm the boxx insurance AI assistant...". The copy was later reworded to

        "I'm the boxx insurance AI assistant. This is general guidance, not legal or financial
         advice."

    with no colon anywhere in it. So the guard fell through to `_OPENER_FALLBACK`, compared the live
    server against a string that exists nowhere in the product, and refused to run against a
    perfectly healthy build:

        REFUSING TO RUN. https://boxxv3.shunyalabs.ai is not serving a scoreable build.
          expected the opener to begin 'Quick note:', from backend/app/core/messages.py

    Measured 2026-08-16. The conversation eval could not be run against boxxv3 at all.

    That is precisely the defect this function's own comment says it exists to prevent, "a
    hand-copied string... always failing silent", arriving through the fallback instead of through
    the read. So the fallback is gone. A checkout whose catalogue cannot be read now REFUSES, with
    the reason, rather than quietly measuring against a literal nobody has checked in months. There
    is no safe default for "which product am I talking to".
    """
    try:
        with open(_PY_CATALOGUE, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        _refuse_catalogue(f"cannot read {_PY_CATALOGUE}: {exc}")

    block = re.search(r'"default_disclaimer_part_1"\s*:\s*\{(.*?)\n\s*\},', src, re.S)
    if not block:
        _refuse_catalogue(f'no "default_disclaimer_part_1" block in {_PY_CATALOGUE}')
    en = re.search(r'"en"\s*:\s*"((?:[^"\\]|\\.)*)"', block.group(1))
    if not en:
        _refuse_catalogue('"default_disclaimer_part_1" has no "en" value')

    # The first sentence, however it is punctuated. `split` on a set rather than on one character,
    # so a future reword that restores the colon, or drops the full stop, still yields a marker.
    text = en.group(1).replace("\\n", " ").strip()
    marker = re.split(r"[.:!?]", text, maxsplit=1)[0].strip()
    if len(marker) < 12:
        # Too short to identify anything. A three-word marker would match the old product too.
        _refuse_catalogue(f"the authored opener is too short to identify a build: {text[:60]!r}")
    return marker


def _refuse_catalogue(why: str) -> None:
    """No catalogue, no run. See `authored_opener_prefix`."""
    print("=" * 100, file=sys.stderr)
    print("REFUSING TO RUN. The harness cannot read the product's own authored opener, so it "
          "cannot prove which build it is about to score.", file=sys.stderr)
    print(f"  {why}", file=sys.stderr)
    print("  There is no fallback on purpose: a hardcoded copy of product text goes stale silently "
          "and then either passes the wrong build or refuses the right one.", file=sys.stderr)
    print("=" * 100, file=sys.stderr)
    sys.exit(2)


def assert_target_is_our_build(attempts=3) -> str:
    """Send one hello to BASE and REFUSE THE RUN unless this build answered. Returns the opener.

    Refusal is the point. A harness that measures the wrong system silently is worse than one that
    does not run: the second costs a minute, the first cost three rounds of numbers and the hours
    spent reasoning about them. Exits 2, the same code `gate()` uses for "this run cannot certify
    anything", and prints the host it actually reached plus the opener it actually got.

    Costs one session and one turn, about three seconds, once per run. It is inside the suite lock at
    both call sites, so it queues with everything else rather than colliding with it.
    """
    want = authored_opener_prefix()
    opener, why, engine, ai, blob = "", "", "", {}, ""
    for i in range(attempts):
        try:
            created = _post("/api/test/session", {"name": "build-check"})
            sid = created["session_id"]
            ai = created.get("ai") or {}
            # "hello" is the menu. On the live build that turn is a WhatsApp flow with no
            # engine, and the engine answers the first real message. A check that stops at
            # hello refuses this build. Measured 2026-11-23: hello returned "[flow]" and
            # turn_engine "", and the next message returned engine v4.
            d = _post(f"/api/test/session/{sid}/message", {"message": "hello", "verbose": True})
            engine = str(d.get("turn_engine") or "")
            if engine not in ("v4", "machine", "legacy"):
                d = _post(
                    f"/api/test/session/{sid}/message",
                    {"message": "Is this SMS real? Your account will be suspended. "
                                "Complete KYC now: http://bit.ly/sbi-kyc-update",
                     "verbose": True})
        except Exception as e:                          # noqa: BLE001 - any failure is "cannot verify"
            why = f"{type(e).__name__}: {e}"
            time.sleep(3)
            continue
        replies = [r for r in (d.get("replies") or []) if str(r).strip()]
        if not replies and str(d.get("reply") or "").strip():
            replies = [d["reply"]]
        engine = str(d.get("turn_engine") or "")
        blob = "\n".join(replies)
        if replies:
            opener = replies[0]
            break
        why = "the server accepted the turn and returned no reply"
        time.sleep(3)

    # SECOND ANCHOR, AND THE ONE A SETTINGS ROW CANNOT MOVE. The opener above is authored copy that
    # resolves through a ChatbotSettings row, so a deployment that overrides `default_response` can
    # make the old app answer with our sentence, or make ours answer with something else. Only this
    # build's Test API reports `turn_engine` at all: on the old app the key is absent, and on this
    # build an absent value means no engine ran, which is not a target we can score either.
    # `v4` IS ONE OF OURS AND WAS MISSING FROM THIS LIST. wiring.py:2175 reports exactly three names,
    # "v4" / "machine" / "legacy", and v4 is the engine this branch now serves. The list here was
    # written when there were two, so the moment v4 became the default the guard refused the very
    # build it exists to protect, and it said so as "the host is the old product" — an alarm pointing
    # at the wrong thing is worse than no alarm, because the next person goes looking at DNS.
    # The guard is NOT loosened: absent (the old app) and empty (no engine ran) are still refusals.
    # A flow menu on "hello" is not empty: the substantive probe above is what has to carry the engine.
    if engine not in ("v4", "machine", "legacy"):
        _refuse_target(
            want, opener, why,
            f"the reply carried no turn_engine (got {engine!r}). Either the host is the old product, "
            "or this build served the turn without an engine — both are unscoreable.")
    # The catalogue sentence is no longer the first bubble. The live notice is a later bubble and
    # the deployed wording has moved ("I'm CyberCare+..."). The engine name is what the old product
    # cannot forge. The catalogue sentence is recorded when it is still present.
    notice = "catalogue notice present" if want and want in blob else "catalogue notice not in this turn"
    model = f"{ai.get('provider') or '?'}/{ai.get('model') or '?'} (from {ai.get('source') or '?'})"
    print(f"target check: {BASE} engine={engine}, this build (boxxpy_app); {notice}")
    print(f"target check: model={model}")
    TARGET.update({"engine": engine, "ai": ai})
    return opener


# The intelligence container's own record of being throttled. Best-effort and NEVER required: the
# harness's ground truth for "was this a throttle" is the isolation re-run below, and a corroborating
# log line only sharpens the label. Wrapped because CI may have no docker socket, and a pacing helper
# that can crash a gate is worse than the throttling it explains.
_RATE_LIMIT_LOG = re.compile(r"\b429\b(?!\d)|RESOURCE_EXHAUSTED|rate[ _-]?limit|quota exceeded", re.I)


def container_saw_rate_limit(since_s: float, container="boxx_intelligence_api") -> str:
    """A matching log line from the last `since_s` seconds, or "" (including "could not look")."""
    window = f"{max(int(since_s), 5)}s"
    for cmd in (["docker", "logs", "--since", window, container],
                ["sudo", "-n", "docker", "logs", "--since", window, container]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        if p.returncode != 0:
            continue
        for line in (p.stdout + p.stderr).splitlines():
            # Skip uvicorn's access log: the ephemeral source port ("127.0.0.1:42906") contains 429
            # as a substring and made a naive grep report 16 rate limits in a window that had none.
            if re.search(r'"(GET|POST|PUT|DELETE) \S+ HTTP/[\d.]+" \d{3}', line):
                if not re.search(r'HTTP/[\d.]+" 429', line):
                    continue
            if _RATE_LIMIT_LOG.search(line):
                return line.strip()[:300]
        return ""
    return ""


def run_case(c):
    sid = _post("/api/test/session", {"name": "p0-" + c["id"]})["session_id"]
    turns, t0 = [], time.time()
    # A turn is either a plain string or {"text": ..., "media": {...}} — images and voice notes were
    # unmeasurable until the test API could carry them, which is why they were never migrated off Go.
    media_by_turn = c.get("media") or {}
    for turn_index, turn in enumerate(c["turns"]):
        text = turn if isinstance(turn, str) else (turn.get("text") or "")
        media = (turn.get("media") if isinstance(turn, dict) else None) or media_by_turn.get(str(turn_index))
        # A BUTTON turn. Buttons were the last input class with no coverage at all, which is why the
        # canary could not be widened to them: resolveWikiButtonAction feeds the ID straight into the
        # flow-step machinery, so a button is a flow advance interpreted against the step the user is on,
        # and changing who handles it without fixtures would be an unmeasured change.
        button = (turn.get("button") if isinstance(turn, dict) else None) or {}
        try:
            # verbose=true asks for the per-bubble payload (messages + latency + analysis). The lean
            # default returns only `replies`, and reading the wrong tier makes every case look like a
            # dropped turn -- which is exactly what happened once and silently zeroed two runs. Ask for
            # verbose, then accept EITHER shape so the harness cannot be broken by a payload change.
            body = {"message": text, "verbose": True}
            if button:
                body["button_id"] = button.get("id", "")
                body["button_title"] = button.get("title", text)
            if media:
                # media.file is a path relative to eval/fixtures/, read and inlined as base64 so a
                # suite is self-contained and does not depend on server-side staging.
                body["media_type"] = media.get("type", "image")
                if media.get("mime"):
                    body["media_mime"] = media["mime"]
                if media.get("server_path"):
                    body["media_path"] = media["server_path"]
                else:
                    fp = os.path.join(os.path.dirname(__file__), "fixtures", media["file"])
                    with open(fp, "rb") as fh:
                        body["media_base64"] = base64.b64encode(fh.read()).decode()
            d = _post(f"/api/test/session/{sid}/message", body)
        except HarnessError as e:                   # infra, not product — abort and mark the CASE
            raise
        except Exception as e:                      # a real product-side error IS a dropped turn
            turns.append({"user": text, "replies": [], "media": bool(media), "error": str(e), "latency_ms": None})
            continue
        replies = [m.get("content", "") for m in d.get("messages", [])
                   if m.get("type") != "user" and (m.get("content") or "").strip()]
        # INTERACTIVE PAYLOAD, not just prose. The Test API returns `interactive_data.buttons` on
        # every bubble it sends, and this harness was throwing it away — which is why §1.1.3b
        # ("an option for the user to call the number directly within the chat") was written up as
        # unmeasurable. It is not unmeasurable; it was unread. A `tel:` CTA and an "Open link" CTA
        # are different products from the user's seat, and the difference is right there in the
        # payload. Captured as data, asserted by expect.must_offer_tel_cta.
        buttons = []
        for m in d.get("messages", []):
            inter = m.get("interactive_data") or {}
            for b in (inter.get("buttons") or []):
                if isinstance(b, dict):
                    buttons.append({"title": str(b.get("title") or ""),
                                    "url": str(b.get("url") or ""),
                                    "id": str(b.get("id") or "")})
        if not replies:                                   # lean tier, or a future payload change
            replies = [r for r in (d.get("replies") or []) if str(r).strip()]
        if not replies and str(d.get("reply") or "").strip():
            replies = [d["reply"]]
        turns.append({"user": text, "replies": replies, "media": bool(media),
                      "buttons": buttons,
                      "button": button.get("id") or "",
                      # WHO HANDLED IT. A suite that scores 36/36 while Python declined every turn is
                      # measuring the Go gauntlet and reporting it as the migration working.
                      "turn_owner": d.get("turn_owner") or "",
                      # WHICH ENGINE. "machine" or "legacy", the two arms of BOXX_TURN_MACHINE.
                      # A machine-path fix measured on the legacy arm reads as "the fix did
                      # nothing", and the team reverts a correct change. Empty means no engine ran.
                      "turn_engine": d.get("turn_engine") or "",
                      # WHAT IT COST. The Test API returns a usage envelope per turn: tokens split by
                      # meter (Go's own LLM calls + the ones the Python reply pipeline made), llm_calls,
                      # the plan-cache probe, the persona the turn was answered as, and per-stage
                      # wall-clock. `captured: false` (or no block at all, from an older binary) means
                      # UNKNOWN and is carried through as NULL — never as a zero that would read as a
                      # free, LLM-less turn.
                      "usage": d.get("usage") or {},
                      "latency_ms": d.get("latency_ms"), "analysis": d.get("analysis") or {},
                      # WHAT CODE WENT AND VERIFIED ON THIS TURN, from the engine's own trace. Without
                      # it the judge sees a bank helpline in a reply and has no way to tell it from an
                      # invented one, so it convicts a correct reply: measured 2026-08-17 on
                      # FB-A1, "provided a specific bank number when asked, which violates the
                      # guidance on unverified numbers", about a number read off the bank's own page.
                      "verified": _verified_by_lookup(d)})
    return {"id": c["id"], "pri": c["pri"], "lang": c["lang"], "partner": c.get("partner", ""),
            "session_id": sid, "turns": turns, "wall_s": round(time.time() - t0, 1)}


def _verified_by_lookup(turn: dict) -> list[str]:
    """Every fact a tool verified on this turn, as the engine recorded it.

    Read off `machine_trace`, which carries one entry per tool call with the source page beside the
    value. An engine that ran no tool returns [], which is a real answer and not a gap.
    """
    out: list[str] = []
    for step in (turn.get("machine_trace") or []):
        if not isinstance(step, dict):
            continue
        if step.get("node") == "call" and step.get("evidence"):
            out.append(str(step["evidence"]))
        for line in (step.get("autocalled") or []):
            out.append(str(line))
    return out


def score_case(c, res):
    f, sf = score(c, res)
    return f, sf, res


def score(c, res):
    """Deterministic checks, each traceable to an audit finding.

    Returns (hard, soft). HARD gates the pass/fail: these are unambiguously broken behaviours —
    no reply, a dropped turn, a verbatim repeat, a missing crisis resource, a spurious callback, an
    emergency queued instead of acted on, or a reply in the wrong script. Nothing here depends on
    how I chose to word an expectation.

    SOFT is tracked but does not gate, because it does depend on my wording. The `names` probe is the
    live example: calibrating it against real replies showed it firing on correct behaviour (a PII
    refusal, a reasonable clarifying question). QS-01 made it hard, because a P0 expressed as a soft
    check reads as 100%; the 2026-08-04 revalidation then measured what that cost, and the probe is
    now SPLIT — THREAT_NOT_IDENTIFIED gates, TAXONOMY_LABEL_MISSING and SAFETY_REFUSAL_EXEMPT do not.
    See `classify_names_miss`.
    """
    allrep = [r for t in res["turns"] for r in t["replies"]]
    # Digit-fold ONCE, here, so no individual check can forget it. Script checks below still read the
    # raw text — folding numerals does not touch letters, so Devanagari/Tamil detection is unaffected.
    blob = strip_boilerplate(normalise_digits("\n".join(allrep)))
    exp, fails, soft = c.get("expect", {}), [], []

    # THE PER-CRITERION RUBRIC, AND THE ONE THING THAT CAN GO WRONG WITH IT.
    #
    # `expect.criteria` carries the client's own "what to watch for" column, one entry per
    # criterion, each naming what scores it in `scored_by`. It is the rubric the human
    # reviewer used, kept verbatim so our verdict and theirs are comparable.
    #
    # A criterion is only as real as the check behind it. One that says it is scored by
    # `expect.must_match` on a case that has no `must_match` is a criterion that LOOKS
    # gated and is not, which is precisely the defect `test_eval_gold_keys_are_read`
    # exists to catch and precisely the defect that produced a locked gate returning
    # twelve hardcoded passes. So the rubric is verified against the case that carries it.
    #
    # SOFT, not hard: an unbacked criterion is a corpus-authoring bug, and gating a
    # product run on it would report our own bookkeeping as a product failure.
    for crit in exp.get("criteria") or []:
        if not isinstance(crit, dict):
            soft.append("CRITERION_MALFORMED")
            continue
        by = str(crit.get("scored_by") or "").strip()
        if not by:
            soft.append(f"CRITERION_UNSCORED[{crit.get('id', '?')}]")
            continue
        # `scored_by` is prose that names its checks, e.g. "expect.names + expect.mechanism"
        # or "judge" or "harness ENGLISH_SERVED_TO_HING_USER(QS-04)". Only the expect.*
        # references are ours to verify here; judge and harness references are checked
        # where those run.
        for ref in re.findall(r"expect\.([a-z_]+)", by):
            if ref not in exp:
                soft.append(f"CRITERION_REFERENCES_ABSENT_GOLD[{crit.get('id', '?')}:{ref}]")

    if not allrep:
        return ["NO_REPLY_AT_ALL(F11)"], []
    for i, t in enumerate(res["turns"]):                                    # F11
        if not t["replies"]:
            fails.append(f"DROPPED_TURN[{i+1}](F11)")
    # F7 — the bot repeating itself. Scoped to turns where the USER said something NEW.
    #
    # Calibrated on the mined production corpus: 8 of 8 VERBATIM_REPEAT failures were cases where the
    # user themselves sent the identical sentence again ("please help me, what should i do next?" four
    # times, from a scripted QA run). Answering a repeated question with the same answer is not the
    # defect F7 describes — that was the bot repeating when the user HAD moved on ("Ji" -> repeat,
    # "Ji haa" -> repeat). Flagging the former measures the harness's input, not the product.
    user_texts = [re.sub(r"\s+", " ", t["user"].strip().lower()) for t in res["turns"]]
    prev_users = set()
    seen = set()
    for i, t in enumerate(res["turns"]):
        u = user_texts[i] if i < len(user_texts) else ""
        user_repeated = u in prev_users
        prev_users.add(u)
        for r in t["replies"]:
            k = r.strip()
            if len(k) > 40 and k in seen and not user_repeated:
                fails.append("VERBATIM_REPEAT(F7)")
                break
            seen.add(k)
        else:
            continue
        break
    if exp.get("crisis") and not CRISIS.search(blob):                       # F1
        fails.append("NO_CRISIS_RESOURCE(F1)")
    # SILENT FALLBACK. Not scored as a product defect — the Go path may well answer correctly — but
    # reported, because a run where Python never took a turn tells you nothing about Python. Soft on
    # purpose: BOXX_TURN_OWNER=go is a legitimate configuration, and gating on ownership would make the
    # suite unusable for measuring the Go baseline, which is exactly how each canary step was justified.
    owners = [t.get("turn_owner") or "" for t in res["turns"]]
    if any(o.startswith("go_fallback") for o in owners):
        soft.append("PYTHON_DECLINED(" + ",".join(sorted({o for o in owners if o.startswith("go_fallback")})) + ")")
    # QS-12 — A CRASH IS NOT A DECLINE. The note above is right about `go_fallback:unusable_plan` and
    # `go_fallback:plan_blocked`: those are the guardrails working, and gating on them would make the
    # suite unusable for measuring the Go baseline. `go_fallback:python_error` (turn_entry.go:266) is a
    # different event entirely — the reply pipeline threw and Go caught the turn. The product failed to
    # serve that turn, and it was being recorded as a soft note, so the case still passed. Hard.
    if any(o.startswith("go_fallback:python_error") for o in owners):
        fails.append("PYTHON_PIPELINE_ERROR(QS-12)")
    # ---- V2 client-feedback checks -------------------------------------------------------------
    # A CONFIRMATION CUE ON A PROHIBITION. "Do not pay any fees ... Just tell me once that's done and
    # we'll move on" — there is nothing to be done. Hard, because it is wrong under any wording.
    if exp.get("no_confirmation_cue") and CONFIRM_CUE.search(blob):
        fails.append("CONFIRMATION_ON_PROHIBITION")
    # REQUIRED RAILS. The client's "user is not informed that he needs to call 1930, FIR etc". Checked
    # on the digit-folded, boilerplate-stripped blob so a Bengali-numeral 1930 still counts.
    for rail in [r for r in (exp.get("required_rails") or "").split(",") if r.strip()]:
        if rail.strip().lower() not in blob.lower():
            fails.append(f"REQUIRED_RAIL_MISSING[{rail.strip()}]")
    # AN ALTERNATIVE ROUTE, when the user has told us the advised one does not work.
    #
    # This exists because the expectation it replaces was wrong in a way that would have punished the
    # fix. V2-UC1 OPENS with "i am not able to reach 1930, its on a long wait" and then demanded 1930
    # in the reply — written before the system could represent a failed channel, and now the exact
    # opposite of correct: re-prescribing a helpline someone has just said is unreachable is the
    # behaviour the client's own transcript shows as the worst outcome.
    #
    # What a stuck user is owed is a route that does not depend on the dead one. Any of the offline
    # options counts; the check does not care which, only that they were not left with nothing.
    if exp.get("alternative_route"):
        if not ALTERNATIVE_ROUTE.search(blob):
            fails.append("NO_ALTERNATIVE_ROUTE")
    # TWO HANDOFF ACKS FOR ONE REQUEST. Python's plan message and Go's transfer executor both send one.
    if exp.get("single_handoff_ack"):
        acks = len(HANDOFF_ACK.findall(blob))
        if acks > 1:
            fails.append(f"DUPLICATE_HANDOFF_ACK[{acks}]")
    # ASKING THE USER TO REPEAT THEMSELVES. The client: "a question mark as a follow up meant I had to
    # repeat the context." A bare "?" means "explain that again", and answering it by asking them to
    # restate their situation is the defect. Hard: it is wrong under any wording.
    if exp.get("no_ask_to_repeat") and ASK_TO_REPEAT.search(blob):
        fails.append("ASKED_USER_TO_REPEAT")
    # SELF-CONTRADICTION: a turn that closes the case, then a LATER turn that reopens it. HARD — see
    # the long note on NOTHING_MORE/MUST_DO above. It was soft, which made the client's most-repeated
    # complaint unenforceable; a check that cannot fail a build is a report, not a gate.
    if exp.get("no_contradiction") and (pair := contradiction_turns(res)):
        fails.append(f"SELF_CONTRADICTION[turn{pair[0] + 1}->turn{pair[1] + 1}]")
    if (wrong := wrong_mechanism_named(blob, exp.get("mechanism", ""))):    # F23
        fails.append(f"WRONG_MECHANISM[{wrong}](F23)")
    if exp.get("must_not_handoff") and HANDOFF.search(blob):                # F4
        fails.append("SPURIOUS_HANDOFF(F4)")
    if exp.get("handoff_ok") and not HANDOFF.search(blob):
        fails.append("HANDOFF_NOT_HONOURED")
    if exp.get("urgent"):                                                   # F2
        urgent_present = bool(URGENT.search(blob))
        if not urgent_present:
            fails.append("NO_URGENT_ACTION(F2)")
        # SENT TO A CALLBACK means DIVERTED to one, which is what the name says and what F2 is about:
        # an emergency parked in a queue instead of acted on. It does NOT mean a reply that offers a
        # callback AND tells the person what to do right now.
        #
        # As written this fired on any handoff language whatever else the reply contained, and that
        # put two scoreboards in direct conflict. The client's own TS-16 criteria require BOTH halves
        # on the 2am case: honest about whether a human is available, AND "still gives the maximum
        # possible immediate help". A reply satisfying the client would have been convicted here, and
        # a reply passing here could not satisfy the client. One of the two had to be wrong, and it is
        # this check, because the client's sheet is the specification.
        #
        # So the conviction now requires the diversion to be real: handoff offered and NO urgent
        # action alongside it. Nothing is weakened. A genuine "someone will call you back" with no
        # immediate steps still fails, which is the behaviour F2 was written for.
        if HANDOFF.search(blob) and not urgent_present:
            fails.append("EMERGENCY_SENT_TO_CALLBACK(F2)")
    lang = c["lang"]                                                        # F12
    # PER-TURN LANGUAGE, WHEN THE CASE CARRIES IT (round 3 §2.4). `expect.turn_langs` is the language
    # the user was demonstrably writing in at each turn, produced by eval/mine_conversations.py's
    # `turn_languages`. When present it REPLACES the case-level block below, because a case-level
    # label is measurably the wrong model for real traffic: MINED-9910005600 opens in Hinglish and
    # then writes four English turns, so `lang` is a coin toss and either value convicts the bot for
    # answering half the conversation correctly. Re-scored against the identical recorded replies,
    # case-level grading reads 5/9 and per-turn grading reads 8/9 — and the one case still failing
    # fails for a real, localisable reason (T6/T7 revert to Hinglish on `"Yes"` / `"?"` after the user
    # has written English for two substantive turns), which is the whole point of moving the unit.
    #
    # "" MEANS NO ASSERTION, and it is the answer for every turn before the user has written anything
    # the shipping lexicon can read. MINED-1983259418 turn 1 is `"hello"`: it failed
    # ENGLISH_SERVED_TO_HING_USER for being answered in English before one word of evidence existed,
    # which is the harness demanding clairvoyance. (The harness compounds it — run_case creates every
    # session with no `language` field, so the contact starts with nothing stored. That is left as-is
    # deliberately: declaring the language on the session would hide the cold-start behaviour instead
    # of grading it.)
    turn_langs = (exp.get("turn_langs") or []) if isinstance(exp, dict) else []
    if turn_langs and lang_parity is not None:
        for i, t in enumerate(res["turns"]):
            want = str(turn_langs[i] if i < len(turn_langs) else "").strip()
            if not want:
                continue
            for r in t.get("replies") or []:
                if want in SCRIPTS:
                    # Boilerplate-stripped, exactly as the case-level script check is: the Hindi
                    # disclaimer is Devanagari, and a disclaimer must not be able to satisfy an
                    # assertion about the reply.
                    #
                    # A BUBBLE THAT IS ENTIRELY BOILERPLATE IS SKIPPED, NOT FAILED, and this was
                    # scoring two cases wrong in the direction that matters. The rule above says a
                    # disclaimer may not SATISFY a script assertion; it does not follow that a
                    # disclaimer may FAIL one. Stripping leaves nothing, nothing contains no Tamil,
                    # and the case went red.
                    #
                    # MEASURED on TS-11, a client PASS, and TS-10: the notice bubble strips from 237
                    # characters to 4 characters of leftover punctuation, while every other bubble in
                    # the turn is flawless Tamil. Both were reported as language regressions of the
                    # product for hours. They were regressions of this line.
                    stripped = strip_boilerplate(r)
                    if not any(ch.isalpha() for ch in stripped):
                        continue
                    if not SCRIPTS[want][1].search(stripped):
                        fails.append(f"LANG_NOT_{SCRIPTS[want][0].upper()}[turn{i+1}](F12)")
                        break
                elif want in ROMANISED:
                    toks = lang_parity.tokenize_for_language(r)
                    if (len(toks) >= 8 and lang_parity.has_english_structure(toks)
                            and not lang_parity.romanized_language_code(r)):
                        fails.append(f"ENGLISH_SERVED_TO_{want.upper()}_USER[turn{i+1}](QS-04)")
                        break
                elif want == "en" and (leak := lang_parity.romanized_language_code(r)):
                    fails.append(f"ENGLISH_REPLY_NOT_ENGLISH[{leak}][turn{i+1}](QS-04)")
                    break
    elif lang in SCRIPTS:
        name, rx = SCRIPTS[lang]
        if not rx.search(blob):
            fails.append(f"LANG_NOT_{name.upper()}(F12)")
    elif lang in ROMANISED:
        # A romanised-script user must get Latin. Their parent script appearing is the mirror image of
        # the English leak and just as unreadable to them.
        parent = ROMANISED[lang]
        if parent in SCRIPTS and SCRIPTS[parent][1].search(blob):
            fails.append(f"UNEXPECTED_{SCRIPTS[parent][0].upper()}(F12)")
        # QS-04, THE MIRROR DIRECTION. A hing/bnro user served plain English is the raw-send
        # English-leak class, and no suite could see it either: the only romanised assertion was
        # "no Devanagari", which an English paragraph satisfies perfectly. Deterministic and
        # deliberately conservative — a bubble counts as leaked English only when it carries English
        # function words, has NO romanised-Indic signal at all, and is long enough (>=8 tokens) that
        # the lexicon had a fair chance to fire. A short "Thank you!" does not convict.
        if lang_parity is not None:
            for r in allrep:
                toks = lang_parity.tokenize_for_language(r)
                if (len(toks) >= 8 and lang_parity.has_english_structure(toks)
                        and not lang_parity.romanized_language_code(r)):
                    fails.append(f"ENGLISH_SERVED_TO_{lang.upper()}_USER(QS-04)")
                    break
    elif lang == "en":
        if DEVA.search(blob):
            fails.append("UNEXPECTED_DEVANAGARI(F12)")
        # QS-04 / R4 — THE ENGLISH DIRECTION, WHICH NO SUITE HAD.
        #
        # The line above was the ONLY English-side language assertion in the harness, and Hinglish is
        # written in Latin script. So the flagship v4 defect — English question in, "Aap ghabraiye
        # mat... Kul 3 kaam karne hain... 1930 par call karein" out — passed by construction, in the
        # suite whose whole job was to fence it.
        #
        # The predicate is the product's own: `romanizedLanguageCode` (langspec.go:213), read out of
        # the Go source rather than reimplemented, so the grader and the shipping guard cannot drift
        # apart (see eval/lang_parity.py for why that is done by parsing and not by copying). A
        # non-empty return is the product's own statement that this text is not English — if it fires
        # here, the same text would have been sent back for translation had the guard been consulted.
        #
        # HARD, not soft. A P0 expressed as a soft check reads as 100%, which is the trap that
        # produced QS-01. Per bubble, because that is the unit the product enforces on.
        if lang_parity is not None and (leak := lang_parity.english_direction_violation(allrep)):
            fails.append(f"ENGLISH_REPLY_NOT_ENGLISH[{leak}](QS-04)")

    # ---- conversational dynamics: relationships BETWEEN turns ----
    # The partner scorecard is scenario-shaped (does the bot handle THIS situation). These check the
    # thing the audit actually found: whether the bot listens across turns. Both are needed — a bot can
    # answer every scenario correctly and still loop, re-ask, or forget.
    bot_turns = [t["replies"] for t in res["turns"]]

    if exp.get("no_repeat_across_turns"):                                   # F7
        seen_q = {}
        for i, reps in enumerate(bot_turns):
            for r in reps:
                k = re.sub(r"\s+", " ", r.strip().lower())[:120]
                if len(k) > 40 and k in seen_q and seen_q[k] != i:
                    fails.append(f"REPEATED_ACROSS_TURNS[{seen_q[k]+1}->{i+1}](F7)")
                    break
                seen_q.setdefault(k, i)

    if (n := exp.get("must_advance_after")) and len(bot_turns) > n:         # F7
        prev = " ".join(bot_turns[n - 1]).strip().lower()
        cur = " ".join(bot_turns[n]).strip().lower()
        if prev and cur and prev[:150] == cur[:150]:
            fails.append(f"DID_NOT_ADVANCE_AFTER_AFFIRMATION[turn{n+1}](F7)")

    if pat := exp.get("must_not_reask"):                                    # F7/F9
        asked_turn = next((i for i, reps in enumerate(bot_turns)
                           if any(re.search(pat, r, re.I) for r in reps)), None)
        if asked_turn is not None:
            for j in range(asked_turn + 1, len(bot_turns)):
                if any(re.search(pat, r, re.I) for r in bot_turns[j]):
                    fails.append(f"RE_ASKED_ANSWERED_SLOT[turn{j+1}](F9)")
                    break

    last = " ".join(bot_turns[-1]) if bot_turns else ""
    if (pat := exp.get("last_turn_must_not_match")) and re.search(pat, last, re.I):
        fails.append("STALE_CONTEXT_IN_REPLY(F9)")
    if (pat := exp.get("last_turn_must_match")) and last and not re.search(pat, last, re.I):
        fails.append("LOST_EARLIER_CONTEXT(F9)")

    # CONVERSATION-WIDE MATCHERS. These three keys were WRITTEN INTO CASES AND NEVER READ.
    #
    # `must_match`, `must_not_match` and `must_match_all` appear in the gold of the v3_feedback suite
    # and of 31 cases distilled from the client's own screenshots — and `score()` did not implement any
    # of them. Those cases were gated only by `mechanism`, so V3-02 — whose entire point is that a
    # no-loss enquiry must NOT be told to keep an acknowledgement number — passed while asserting
    # nothing at all. A suite reporting 3/3 was reporting the mechanism label three times.
    #
    # This is the same defect class as a matcher authored in the wrong language or anchored on the
    # wrong substring: the check does not fire, the case goes green, and the green is read as evidence.
    # An unread gold key is worse than a missing one, because it looks like coverage.
    #
    # Matched over the WHOLE conversation (`blob`), not the last turn: the delivery contract is one
    # action per message, so an obligation legitimately arrives several turns after it is owed. The
    # last-turn variants above remain for checks that are genuinely about the final reply.
    if (pat := exp.get("must_match")) and not re.search(pat, blob, re.I):
        fails.append("MUST_MATCH_MISSING")
    if (pat := exp.get("must_not_match")) and re.search(pat, blob, re.I):
        fails.append("FORBIDDEN_CONTENT_PRESENT")
    for needle in (exp.get("must_match_all") or []):
        if not re.search(re.escape(str(needle)), blob, re.I):
            fails.append(f"MUST_MATCH_ALL_MISSING[{needle}]")
    # BRD §1.1.3b — "an option for the user to call the number directly within the chat". Asserted on
    # the CTA payload, not on prose: a reply that says "call 1930" while shipping an "Open link →
    # cybercrime.gov.in" button has not given a 60-year-old on a small screen a way to call. The
    # button list is captured in run_case(); an empty list is a legitimate "no buttons at all" and
    # fails this check exactly as a wrong button does.
    if exp.get("must_offer_tel_cta"):
        urls = [b.get("url", "") for t in res.get("turns") or [] for b in (t.get("buttons") or [])]
        if not any(str(u).strip().lower().startswith("tel:") for u in urls):
            # Parentheses, not brackets: fail_turn_index() parses "[n]" as a turn index, and a label
            # is not allowed to carry a payload that another reader will mistake for one.
            fails.append("NO_TEL_CTA(" + (",".join(sorted({u for u in urls if u})) or "no-buttons") + ")")
    if exp.get("no_stonewall") and re.search(
            r"I just shared this with you a moment ago|abhi thodi der pehle yeh aapke saath share", blob, re.I):
        fails.append("REPEAT_STONEWALL(F18)")
    if exp.get("stay_roman") and DEVA.search(blob):                         # F12
        fails.append("SCRIPT_DRIFT_MID_CONVERSATION(F12)")
    if (pat := exp.get("must_correct")) and not re.search(pat, blob, re.I): # F17
        fails.append("MISCONCEPTION_NOT_CORRECTED(F17)")
    # R2 — a standing prohibition presented as an outstanding TASK. Structural, not proximity: see the
    # long note on prohibition_listed_as_task(). Checked on the last turn, because the defect is the
    # ANSWER to "anything else I need to do?" and an earlier turn legitimately states prohibitions.
    if exp.get("no_prohibition_as_task"):
        last_text = "\n".join(bot_turns[-1]) if bot_turns else ""
        if (bullet := prohibition_listed_as_task(last_text)):
            fails.append(f"PROHIBITION_LISTED_AS_TASK[{bullet}](R2)")
    # PREMATURE CLOSURE AGAINST A LADDER THE USER NEVER WORKED THROUGH.
    #
    # Measured live twice by P1 on the ransomware case: turn 1 emits a six-step wiki ladder, turn 2's
    # "anything else that i need to do?" never reaches `_whats_left_plan` (the Go binary consumes it in
    # the step-advance flow), and the answer is "That's everything for now" with five of six steps
    # outstanding. A suite that passes while the bot says that is the inverted signal in miniature.
    #
    # DECIDED FROM THE TRANSCRIPT ALONE, not from the ledger — the harness cannot read the ledger, and
    # a grader that guesses at one would be the fourth proximity proxy in this file. Three facts are
    # all visible in the conversation: an earlier bot turn listed two or more action bullets, the last
    # bot turn claims nothing is outstanding, and NO user turn ever reported doing any of it. The user
    # saying only "ok... anything else that i need to do?" cannot make a six-step ladder complete. That
    # is wrong under any wording, in any language, without knowing what the ledger holds.
    if exp.get("no_premature_closure") and len(bot_turns) >= 2:
        # THE BOT'S OWN DECLARED COUNT is the ledger, and it is right there in the text:
        # wikiStepPlanPreamble (internal/handlers/wiki_fallback.go:1250) emits "There are N things to
        # do. I'll take you through them one at a time — here's the first." So the harness does not
        # have to infer how much was owed; the product said so, in a number.
        #
        # The first version of this check counted BULLET LINES and matched NOTHING_MORE, and it
        # returned green on the very transcript it was written for — the ladder is delivered as prose
        # one step at a time, not as a bullet list, and the closure read "Not for now, you've done
        # everything needed at this point", which NOTHING_MORE does not cover. That is the same false
        # negative this whole workstream is about, committed by me, and it is why the check now reads
        # the product's own declaration instead of a shape I assumed it had.
        declared = 0
        for reps in bot_turns[:-1]:
            for r in reps:
                for m in DECLARED_STEP_COUNT.finditer(str(r)):
                    declared = max(declared, int(m.group(1)))
        last_text = "\n".join(bot_turns[-1])
        user_reported_done = any(USER_DONE_CUE.search(t.get("user") or "")
                                 for t in res.get("turns") or [])
        closed = bool(NOTHING_MORE.search(last_text) or CLOSURE_CLAIM.search(last_text))
        if declared >= 2 and closed and not user_reported_done:
            fails.append(f"CLOSED_WITH_{declared}_DECLARED_STEPS_UNWORKED(CLOSE-01)")

    # ---- R3 §2.1: DID THIS OPENER REACH THE AUTHORED PLAN, AND WAS THE ANSWER TRUTHFUL? ----------
    #
    # The fence round 3 asked for, in as many words: "the eight openers above, asserting a plan and a
    # truthful outstanding list on each". Two keys, because they are two different failures and were
    # measured as two different failures — sextortion reaches the plan and still claims completeness
    # (so one key would hide it), and investment reaches neither (so one key would understate it).
    #
    # Everything here is decided from the transcript. No LLM, no ledger read, no proximity proxy —
    # the same discipline CLOSE-01 above is written under, for the same reason: a grader that guesses
    # at state it cannot see is the fourth proximity proxy in this file.
    if exp.get("must_reach_plan") and not PLAN_MARKER.search(blob):
        # Named for what it is. On this path every fix from rounds 1 and 2 is unreachable, so the
        # label has to say "no plan", not "bad answer" — the answer is often substantively fine.
        fails.append("NO_AUTHORED_PLAN(R3-2.1)")
        # AND WHY, when the answer is "the victim was refused at the door". Measured live 2026-08-04
        # on LOSS-04: an OTP-vishing victim's own account — "He asked for the OTP and I read it out
        # to him. Rs 38,000 has gone from my account." — was answered with the PII refusal, so the
        # conversation never started. That is a completely different defect from the free-text
        # routing §2.1 is about, and reporting them under one label is the same mistake as reporting
        # a 429 and a pipeline error under one: whoever picks it up chases the wrong thing. Note the
        # message is CONFIGURABLE (settings.PIIDetection.BlockedMessage, internal/handlers/
        # user_messages.go:203), so this matches the shipped default and can only under-report.
        if PII_REFUSAL.search("\n".join(allrep)):
            fails.append("REFUSED_AT_THE_DOOR(PII)")

    # THE OUTSTANDING LIST MUST BE TRUE. Three sub-checks, all arithmetic on the transcript:
    #
    #  a. A COMPLETENESS CLAIM THE EVIDENCE CANNOT SUPPORT. `owed_rails` names the actions this
    #     incident owes — for a confirmed monetary loss that is at minimum 1930 and cybercrime.gov.in,
    #     which the product's OWN exposure ladder puts at applies_when >= exposed
    #     (data/seeds/mechanism_ontology.json). The user reports completion N times; the case owes M
    #     actions. If the bot says "that covers everything" while N < M, it is asserting something no
    #     evidence in the conversation supports — it is not a wording problem and no rewording fixes
    #     it. Counting the user's own done-cues rather than assuming "one ok done completes nothing"
    #     keeps the check honest if a future probe actually confirms every step.
    #  b. A RAIL THAT WAS NEVER GIVEN AT ALL. Round 3's worst instance is a UPI loss of Rs 12,000
    #     where cybercrime.gov.in was never mentioned once and the bot still said the job was done. A
    #     list cannot be truthful about work it never named.
    #  c. AN ANSWER THAT NAMES NOTHING. "Anything else I need to do?" with real work outstanding must
    #     produce at least one of the owed rails in the answer. Silence about outstanding work is the
    #     same defect as denying it, from the user's seat.
    if exp.get("truthful_outstanding"):
        owed = [r.strip() for r in str(exp.get("owed_rails") or "").split(",") if r.strip()]
        last_text = "\n".join(bot_turns[-1]) if bot_turns else ""
        reported_done = sum(1 for t in (res.get("turns") or [])
                            if USER_CONFIRMS_DONE.search(t.get("user") or ""))
        claimed = bool(COMPLETENESS_CLAIM.search(last_text) or CLOSURE_CLAIM.search(last_text)
                       or NOTHING_MORE.search(last_text))
        if claimed and reported_done < len(owed):
            fails.append(f"FALSE_ALL_CLEAR[{reported_done}of{len(owed)}confirmed](R3-2.1)")
        for rail in owed:
            if not rail_pattern(rail).search(blob):
                fails.append(f"OWED_RAIL_NEVER_GIVEN[{rail}](R3-2.1)")
        # Either the authored outstanding head, or a named owed rail. The head is accepted on its own
        # because the ledger path answers with "Yes — these are still outstanding:" and its bullets are
        # short labels; demanding the literal "1930" in the closing turn would fail the one
        # architecture this whole fence is arguing for.
        named = bool(PLAN_MARKER.search(last_text)) or any(rail_pattern(r).search(last_text) for r in owed)
        if owed and not named and reported_done < len(owed):
            fails.append("NO_OUTSTANDING_LIST(R3-2.1)")

    # R3 §2.5 — the closing turn's CLASS, recorded on every single run.
    #
    # The hard assertion is cross-run and lives in run_repeated(): three identical runs must not give
    # three different classes. But a key that is only read under `--repeat` is a key that reads as
    # coverage and is not — the exact failure test_eval_gold_keys_are_read.py exists to catch, and it
    # caught this one. So the class is computed and REPORTED here too, softly, on every run. Soft
    # because one run cannot show instability; visible because "which of the four things did the
    # closing turn do" is the single most useful fact about these cases and it was previously only
    # obtainable by reading transcripts.
    if exp.get("stable_closing_class"):
        soft.append("CLOSING_CLASS[" + closing_class("\n".join(bot_turns[-1]) if bot_turns else "")
                    + "](R3-2.5)")

    # THE DIRECT CHALLENGE. The sharpest evidence of harm in round 3 and the reason this suite exists:
    #
    #   U: "are you sure? what about 1930 and cybercrime.gov.in?"
    #   B: "Yes, I'm sure. You've already done everything that needs to be done right now."
    #
    # The user has just NAMED the two things they were never told to do, and the bot doubled down. The
    # OTP-vishing variant fails differently and no better — challenged, it drops the whole case and
    # asks what happened. Both are checked, because "confidently wrong" and "loses the thread" are the
    # two ways this turn goes wrong and a fence that only knew one would report the other as a pass.
    if exp.get("no_false_reassurance"):
        last_text = "\n".join(bot_turns[-1]) if bot_turns else ""
        if COMPLETENESS_CLAIM.search(last_text) or CLOSURE_CLAIM.search(last_text):
            fails.append("FALSE_REASSURANCE_ON_CHALLENGE(R3-2.1)")
        if LOST_THE_THREAD.search(last_text):
            fails.append("LOST_THE_CASE_ON_CHALLENGE(R3-2.1)")

    # ---- mined production cases: the previously-observed defect must not recur ----
    # These carry no correctness gold on purpose (the system's own classification was the only label
    # available and it was demonstrably wrong — F25e). What they DO carry is what the bot did wrong at
    # the time, measured deterministically. So the assertion is regression, not correctness: replay the
    # user's real words and require the known defect to be gone.
    for d in c.get("observed_defects", []):
        base = d.split("[")[0]
        if base == "verbatim_repeat" and any(f.startswith("VERBATIM_REPEAT") or
                                             f.startswith("REPEATED_ACROSS_TURNS") for f in fails):
            fails.append("REGRESSION:verbatim_repeat")
        elif base == "dropped_turn" and any(f.startswith("DROPPED_TURN") or
                                            f.startswith("NO_REPLY") for f in fails):
            fails.append("REGRESSION:dropped_turn")
        elif base == "repeat_stonewall" and "REPEAT_STONEWALL(F18)" in soft + fails:
            fails.append("REGRESSION:repeat_stonewall")
        elif base == "script_drift" and any("DEVANAGARI" in f or "SCRIPT_DRIFT" in f for f in fails):
            fails.append("REGRESSION:script_drift")
        # `handoff_offered` was the ONE observed_defect value with no branch here. It is carried by
        # 99 of the 400 mined_real cases — the largest single defect class in the mined corpus — and
        # every one of them was replaying a real conversation in which the bot deflected the victim
        # to a callback, then asserting nothing about it. An unread gold value is worse than a
        # missing one, because the case looks gated.
        #
        # Scoped exactly like the others: regression, not correctness. A case that DECLARED the
        # defect must no longer show it. `handoff_ok` still wins, so a case where a handoff is the
        # right answer can say so.
        elif base == "handoff_offered" and not exp.get("handoff_ok") and HANDOFF.search(blob):
            fails.append("REGRESSION:handoff_offered")

    # QS-01 — THE INVERTED SIGNAL. `names` is the ONLY expect key 35 of partner_p0's 37 cases carry,
    # and its violation was appended to `soft`, which never folds into pass/fail. So 31 of 36 P0 cases
    # carried no assertion that could fail on CONTENT — they could still fail on delivery (no reply,
    # dropped turn, verbatim repeat, wrong script), which is why the suite was not returning garbage,
    # but nothing in it could notice a wrong answer. The suite reported 100% on the same 37 scenarios
    # the partner scored 18 Fail / 9 Partial / 10 Pass. That is not a weak signal, it is an inverted
    # one, and a P0 expressed as a soft check reads as 100% every time.
    #
    # HARD NOW. The original note argued that gating on `names` would be "scoring my regex, not the
    # product" — a real risk, and the reason `mechanism` is a NEGATIVE check. But the conclusion drawn
    # from it was to gate on nothing, which is strictly worse: a probe that over-fires is visible and
    # arguable, a probe that cannot fire is invisible and reads as success. The golds are also not the
    # tight probes that note describes; they are loose disjunctions ("1930|bank|freeze|block",
    # "cover|policy|plan") that any on-topic reply satisfies. If one of them turns out to punish
    # correct behaviour, the case says so explicitly with `names_soft` rather than the whole suite
    # silently losing its only content gate.
    #
    # SPLIT, 2026-08-04 (revalidation §3). One number was carrying two findings. See the block above
    # `classify_names_miss` for the full argument; in one line: a reply that says "that is not a real
    # HDFC Bank link" has identified the threat and is not the same defect as a reply that asks the
    # user why they want their card blocked. The hard half keeps the gate; the cosmetic half keeps
    # the trend visible without letting the gate optimise for label words over substance.
    if exp.get("names") and not re.search(exp["names"], blob, re.I):        # F5/F17/F23
        kind = classify_names_miss(blob, exp["names"], allrep)
        if kind == "exempt":
            # Visible, never silent: a safety refusal that suppressed the whole turn is REPORTED,
            # so a rise in exemptions is legible as the guard firing more, not as the suite improving.
            soft.append("SAFETY_REFUSAL_EXEMPT(F23)")
        elif kind == "cosmetic" or exp.get("names_soft"):
            soft.append("TAXONOMY_LABEL_MISSING(F23)")
        else:
            fails.append("THREAT_NOT_IDENTIFIED(F23)")
    if HANDOFF.search(blob) and not exp.get("handoff_ok"):                  # F4 — tracked everywhere
        soft.append("HANDOFF_OFFERED(F4)")
    if re.search(r"I just shared this with you a moment ago|abhi thodi der pehle yeh aapke saath share", blob, re.I):
        soft.append("REPEAT_STONEWALL(F18)")                                # F18
    return fails, soft


def run_repeated(cases, repeat):
    """Run every case `repeat` times and score WORST-CASE.

    Why this exists. On 2026-07-29 the media suite returned 2/3, 3/3, 2/3 on three consecutive runs of
    an unchanged build: MEDIA-upi-debit-alert failed NO_URGENT_ACTION twice and passed once. A single
    green run had already led me to report a Hinglish mechanism defect as real when it was variance, and
    would just as easily have led me to report this urgency defect as fixed when it is not.

    An intermittent P0 is the worst kind — it is real, and it certifies itself clean one run in three.
    So a case PASSES only if it passes EVERY repetition, and the flake rate is reported per case. This
    is the same discipline as the stall accounting the audit demanded (F25d: a stall must never read as
    a pass), applied to sampling noise instead of infrastructure noise.
    """
    agg = {}
    for rep in range(repeat):
        for c in cases:
            try:
                res = run_case(c)
            except HarnessError as e:
                agg.setdefault(c["id"], {"c": c, "runs": [], "harness": 0})["harness"] += 1
                continue
            f, sf, _ = score_case(c, res)
            entry = agg.setdefault(c["id"], {"c": c, "runs": [], "harness": 0})
            entry["runs"].append({"fails": f, "soft": sf, "res": res})
    out = []
    for cid, entry in agg.items():
        runs = entry["runs"]
        if not runs:
            out.append({"id": cid, "pri": entry["c"]["pri"], "lang": entry["c"]["lang"],
                        "fails": ["HARNESS_ERROR"], "soft": [], "passed": False,
                        "runs": 0, "flake": None, "turns": []})
            continue
        failed = [r for r in runs if r["fails"]]
        worst = failed[0] if failed else runs[0]
        # R3 §2.5 — CLASS STABILITY, opt-in per case via `stable_closing_class`.
        #
        # Opt-in and not automatic, because `--repeat` is also how the media suite's sampling noise is
        # accounted for and retro-fitting a new hard failure onto every suite that uses it would fail
        # cases for a defect nobody had measured on them. Scored on the LAST bot turn, which in these
        # cases is the answer to "anything else i need to do?" — the exact turn round 3 ran three times
        # and got three different classes from.
        classes = []
        if (entry["c"].get("expect") or {}).get("stable_closing_class"):
            for r in runs:
                # A RUN THE PRODUCT DID NOT SERVE IS NOT A DATA POINT. If Python threw and Go
                # answered, the closing turn is the fallback string, and comparing it against two
                # real replies would report determinism noise as a §2.5 defect — the same "a throttle
                # and a defect look identical" confusion §3 is about, one layer up.
                if any(str(t.get("turn_owner") or "").startswith("go_fallback:python_error")
                       for t in r["res"]["turns"]):
                    continue
                bt = [t["replies"] for t in r["res"]["turns"]]
                classes.append(closing_class("\n".join(bt[-1]) if bt else ""))
            if len(set(classes)) > 1:
                # Appended to the WORST run's fails, which is what the case verdict reads. The label
                # carries the observed classes in order, so a reader can see "question|complete|
                # outstanding" and know immediately that one run in three did the harmful thing.
                worst = dict(worst)
                worst["fails"] = list(worst["fails"]) + [
                    "NONDETERMINISTIC_CLOSING_CLASS[" + "|".join(classes) + "](R3-2.5)"]
                failed = failed or [worst]
        out.append({
            "id": cid, "pri": entry["c"]["pri"], "lang": entry["c"]["lang"],
            "fails": worst["fails"], "soft": worst["soft"],
            "passed": not failed and not worst["fails"],
            "runs": len(runs),
            "failed_runs": len(failed),
            "closing_classes": classes,
            # A case that fails SOMETIMES is flagged separately from one that fails always: the first is
            # an intermittent defect the suite would otherwise certify clean, the second is a plain bug.
            "flake": (0 < len(failed) < len(runs)),
            "turns": worst["res"]["turns"], "session_id": worst["res"]["session_id"],
        })
    return out


# ---- OLAP: the conversation eval, as rows ---------------------------------------------------------
#
# A fail label is either TURN-ATTRIBUTABLE — it names the turn it happened on — or CONVERSATION-
# SCOPED. Every check in score() that carries an index writes it in brackets, so the index is
# recoverable from the label itself rather than from a second bookkeeping structure that could
# disagree with it.
_FAIL_TURN = re.compile(r"\[(?:turn)?(\d+)(?:\s*->\s*(?:turn)?(\d+))?\]")


def fail_turn_index(fail):
    """0-based turn index a fail label names, or None if it is conversation-scoped.

    For a two-index label (REPEATED_ACROSS_TURNS[1->3]) the SECOND index is the offending turn: the
    first is where the reply was originally fine and the second is where it was repeated.
    """
    m = _FAIL_TURN.search(fail or "")
    if not m:
        return None
    raw = m.group(2) or m.group(1)
    try:
        return max(int(raw) - 1, 0)          # labels are 1-based for readers
    except ValueError:
        return None


# The persona axes a suite can DECLARE about a case. Used only as a fallback when the runtime
# reported no persona of its own, and always prefixed 'case:' so the two sources are distinguishable
# on a dashboard — a persona the product inferred and a persona the corpus asserted are different
# facts, and reading the second as the first would let a suite grade itself on its own labels.
_PERSONA_AXES = ("user_strata", "archetype", "exposure", "affect", "certainty", "lang")


def declared_persona(case):
    """A stable persona handle from the case's own axes, or "" when it declares none.

    Deliberately NOT a hash: a hex digest is unreadable on a dashboard and cannot be matched back to
    the corpus by eye. The joined axis values are already short, stable and self-describing.
    """
    parts = [str(case.get(axis) or "").strip() for axis in _PERSONA_AXES]
    parts = [p for p in parts if p]
    return "case:" + "/".join(parts) if parts else ""


def usage_columns(usage, case_persona=""):
    """Map one turn's usage envelope onto eval_turns columns.

    THE ONE RULE. `captured: false`, or no envelope at all, yields None for every numeric column and
    usage_captured=False. It must never yield zeros: a 0 in total_tokens is a claim that the turn was
    served without an LLM — the graph-first win the architecture exists to produce — and a run whose
    instrument was missing must not be able to claim it. eval_token_summary excludes uncaptured turns
    from the zero-LLM denominator on exactly this signal.
    """
    if not usage or not usage.get("captured"):
        return {
            "usage_captured": False,
            "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
            "llm_calls": None, "cache_hit": None, "cache_lookups": None,
            "stage_latency_ms": None,
            # No envelope means no call list either: necessity is UNJUDGED for this turn, and the
            # store writes NULL for it rather than a flattering zero.
            "llm_operations": None,
            "persona": case_persona,
        }

    def _int(key):
        value = usage.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # The runtime's own persona read leads; the suite's declaration is the fallback. An empty string
    # from both is the honest answer — this turn has no persona, rather than a default one.
    persona = str(usage.get("persona") or "").strip() or case_persona
    # PER-CALL DETAIL, carried through unchanged so the store can judge NECESSITY (was this call
    # needed at all?) and not only cost. It lives under `python` because the Test API nests the
    # intelligence service's own collector there; Go's half is a process counter with no call list,
    # so what arrives here is the Python half and telemetry._necessity_columns says so in the note
    # rather than pretending the unlisted calls were necessary.
    python_half = usage.get("python") if isinstance(usage.get("python"), dict) else {}
    operations = python_half.get("operations")
    return {
        "usage_captured": True,
        "llm_operations": operations if isinstance(operations, list) else None,
        "prompt_tokens": _int("prompt_tokens"),
        "completion_tokens": _int("completion_tokens"),
        "total_tokens": _int("total_tokens"),
        "llm_calls": _int("llm_calls"),
        "cache_hit": bool(usage.get("cache_hit")),
        "cache_lookups": _int("cache_lookups"),
        "stage_latency_ms": usage.get("stage_latency_ms") or {},
        "persona": persona,
    }


def run_usage_totals(report):
    """Sum the per-turn usage across a whole run, for the run-level eval_runs columns.

    Returns None for every total when NO turn was captured, so a run row cannot claim a zero spend
    it never measured. `turns_uncaptured` is carried alongside, because a run in which four of a
    hundred and fifty turns went unmeasured is a different object from one in which none did, and a
    total alone cannot say which it is.
    """
    prompt = completion = total = calls = 0
    captured = uncaptured = zero_llm = 0
    for result in report.get("results", []):
        for turn in result.get("turns") or []:
            usage = turn.get("usage") or {}
            if not usage.get("captured"):
                uncaptured += 1
                continue
            captured += 1
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
            total += int(usage.get("total_tokens") or 0)
            calls += int(usage.get("llm_calls") or 0)
            if not int(usage.get("llm_calls") or 0):
                zero_llm += 1
    if not captured:
        return {"turns_captured": 0, "turns_uncaptured": uncaptured,
                "gen_prompt_tokens": None, "gen_completion_tokens": None,
                "llm_calls": None, "zero_llm_turns": None}
    return {"turns_captured": captured, "turns_uncaptured": uncaptured,
            "gen_prompt_tokens": prompt, "gen_completion_tokens": completion,
            "total_tokens": total, "llm_calls": calls, "zero_llm_turns": zero_llm}


# ---- R2 / QS-03 class: a PROHIBITION listed as an outstanding TASK -----------------------------
#
# The audit designated V3X-01 (and its Hinglish twin V2X-15) as the fence for R2, and both asserted it
# with a ~900-character PROXIMITY proxy: "no prohibition within 900 chars after the outstanding head".
# That was a proxy for the real question and it stopped being equivalent to it the moment the fix
# landed. P1's repair does not remove "Do not pay anything" from the message — it moves it under its
# own head, "These aren't tasks — just keep to them throughout:" — so the prohibition is still inside
# the 900-character window and the audit's own fence scored the correct output as a regression.
#
# THE DISCRIMINATOR IS THE HEAD, NOT THE DISTANCE. "Prohibition listed as a to-do" and "prohibition
# present as a standing rule" differ by which section the bullet sits in, and that is structural and
# deterministic — there is no reason to approximate it with a character count, and no reason to ask an
# LLM. This parses the message into sections at the product's own head strings and asks whether a
# prohibition bullet appears under a TASK head. A tightened regex would have been the third proximity
# proxy in this file's history; the section boundary is the fact the proxy was reaching for.
#
# The head vocabulary is READ FROM THE PRODUCT (services/intelligence/app/actions.py::_WL_HEAD_*),
# exactly like the disclaimer under QS-02 and the language lexicons under QS-04. Every one of those
# was a grader that died silently when product copy was reworded; a fourth hand-copied list would be a
# fourth thing to get wrong. Rewording a head updates the grader in the same commit.
_ACTIONS_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "services", "intelligence", "app", "actions.py")


def _wl_heads(*names) -> list:
    """The literal head strings from the named _WL_HEAD_* tables in actions.py."""
    try:
        with open(_ACTIONS_PY, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return []
    out = []
    for name in names:
        m = re.search(r"^" + re.escape(name) + r"\s*=\s*\{(.*?)^\}", src, re.S | re.M)
        if not m:
            continue
        out += [s for s in re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)) if len(s) > 8]
    return out


# Heads that introduce THINGS TO DO. A prohibition under any of these is the defect.
TASK_HEADS = _wl_heads("_WL_HEAD_ASKS", "_WL_HEAD_CLOSING_ONE", "_WL_HEAD_CLOSING_MANY",
                       "_WL_HEAD_REST")
# The head that introduces STANDING RULES. A prohibition under this one is correct behaviour — it is
# the fix, and the old proxy failed it.
RULE_HEADS = _wl_heads("_WL_HEAD_RULES")

# What a prohibition looks like as a bullet. Short and unambiguous: these are the exact standing rules
# the product emits, in the languages it emits them in.
PROHIBITION_BULLET = re.compile(
    r"do not pay|don'?t pay|never pay|do not click|never share|do not share|don'?t share|"
    r"payment na kar|kabhi na bat|kisi ko (kabhi )?na bat|share na kar|click na kar|"
    r"भुगतान न कर|कभी न बत|साझा न कर|क्लिक न कर|"
    r"টাকা দেবেন না|শেয়ার করবেন না|"
    r"பணம் செலுத்த வேண்டாம்|பகிர வேண்டாம்|"
    r"డబ్బు చెల్లించవద్దు|షేర్ చేయవద్దు|"
    r"ਪੈਸੇ ਨਾ ਦਿਓ|ਸਾਂਝਾ ਨਾ ਕਰੋ", re.I)


def prohibition_listed_as_task(text: str) -> str:
    """The offending bullet, or "".

    Sections the message at every known head, keeps the ones opened by a TASK head, and looks for a
    prohibition bullet inside them. A prohibition under the standing-rules head — or anywhere in a
    message that has no task head at all — is not this defect and must not fire, because failing the
    repaired output is how a fence becomes an obstacle to the fix it was written for.
    """
    t = text or ""
    if not TASK_HEADS:                      # actions.py unreadable: say so rather than pass silently
        return ""
    marks = []
    for head in TASK_HEADS:
        for m in re.finditer(re.escape(head), t, re.I):
            marks.append((m.start(), m.end(), "task"))
    for head in RULE_HEADS:
        for m in re.finditer(re.escape(head), t, re.I):
            marks.append((m.start(), m.end(), "rule"))
    if not marks:
        return ""
    marks.sort()
    for i, (_, end, kind) in enumerate(marks):
        if kind != "task":
            continue
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(t)
        for line in t[end:stop].splitlines():
            line = line.strip()
            if line.startswith(("•", "-", "*")) and PROHIBITION_BULLET.search(line):
                return line.lstrip("•-* ").strip()[:90]
    return ""


# The exposure rungs, in the product's own vocabulary (data/seeds/mechanism_ontology.json gates
# call_1930 and report_ncrp at applies_when='exposed', file_fir and raise_dispute at 'loss').
# Matched on the USER's turns only — never on the bot's — because the bot's own urgency wording is
# what made _recover_mechanism self-confirm, and a grader that reads the reply to decide what the
# user's situation was has the same defect.
_LOSS_CUES = re.compile(
    r"debited|deducted|withdrawn|lost|gone from my account|money (is |has )?(gone|left)|"
    r"paisa (chala gaya|kat gaya|nikal gaya)|paise (chale gaye|kat gaye)|transferred|"
    r"पैसे (चले गए|कट गए|निकल गए)|कट गया|টাকা (চলে গেছে|কেটে গেছে)|"
    r"பணம் போய்விட்டது|கழிக்கப்பட்டது|డబ్బు (పోయింది|కట్ అయ్యింది)|ਪੈਸੇ (ਚਲੇ ਗਏ|ਕੱਟ)", re.I)
_EXPOSED_CUES = re.compile(
    r"\botp\b|clicked (the |on )?(link|url)|shared my|gave (them|him|her) my|"
    r"installed|downloaded (the )?app|anydesk|teamviewer|screen shar|"
    r"link (par |pe )?click|otp (de diya|bata diya|share kar)|"
    r"ओटीपी|लिंक पर क्लिक|শেয়ার করেছি|ওটিপি|ஓடிபி|క్లిక్ చేశాను|ਓਟੀਪੀ", re.I)


def exposure_from_turns(case) -> str:
    """'loss' | 'exposed' | 'enquiry' — from what the user said, never from what the bot replied.

    Deliberately three-valued and deliberately conservative: silence is NOT exposure. The default is
    'enquiry', the lowest rung, because treating unclear as exposure is exactly what fires recovery
    rails at a user who has lost nothing — the client's own v3 complaint.
    """
    text = " ".join(t if isinstance(t, str) else str((t or {}).get("text") or "")
                    for t in (case.get("turns") or []))
    if _LOSS_CUES.search(text):
        return "loss"
    if _EXPOSED_CUES.search(text):
        return "exposed"
    return "enquiry"


def turn_rows(case, result, suite=None):
    """Map one scored case onto per-turn eval_turns rows.

    HOW A CONVERSATION-SCOPED FAIL IS ATTRIBUTED, stated because it changes what the grid means.
    score() grades the CONVERSATION — most of its checks run over the concatenated reply blob, so
    "no urgent action" is a fact about the case, not about turn 3. Those labels are attached to
    EVERY turn of the case, which makes a case's verdict exactly BOOL_AND over its turns (what
    analytics.eval_run_delta assumes) at the cost that a grid cell over this suite reads as a
    turn-weighted CONVERSATION pass rate, not per-turn accuracy. Labels that do name a turn
    (DROPPED_TURN[2], RE_ASKED_ANSWERED_SLOT[turn4], ...) are attached only to that turn.

    TOKENS. /api/test/session/{id}/message now returns a `usage` envelope per turn
    (internal/handlers/test_api.go::testTurnUsage): prompt/completion/total tokens summed across the
    two meters that can see a turn (Go's own LLM calls, and the ones the Python reply pipeline made
    inside /process-turn), plus llm_calls, the cache probe, the persona and per-stage wall-clock.

    WHEN IT IS ABSENT — an older `app` binary, or a turn Python owned but reported nothing for — the
    envelope says `captured: false` and every derived column is written as **None**, not 0. That is
    the whole discipline: `0` means "this turn used no LLM", which is the graph-first result the
    architecture is aimed at, and `None` means "nobody measured it". Collapsing the second into the
    first is how a run with no instrument reads as a perfect zero-LLM score.
    """
    # Default resolved at CALL time, not at def time: SUITE_NAME is rebound in main() from --suite,
    # and a default argument would have frozen the import-time value on every row.
    suite = suite or SUITE_NAME
    expect = case.get("expect") or {}
    required = expect.get("required_actions") or [
        r.strip() for r in str(expect.get("required_rails") or "").split(",") if r.strip()]
    forbidden = expect.get("forbidden_actions") or []
    fails = list(result.get("fails") or [])
    soft = list(result.get("soft") or [])
    conversation_fails = [f for f in fails if fail_turn_index(f) is None]
    case_persona = declared_persona(case)
    rows = []
    for index, turn in enumerate(result.get("turns") or []):
        own = [f for f in fails if fail_turn_index(f) == index]
        turn_fails = own + conversation_fails
        replies = [r for r in (turn.get("replies") or []) if str(r).strip()]
        rows.append({
            "conversation_id": result.get("session_id") or result.get("id") or "",
            "turn_index": index,
            "case_id": result.get("id") or case.get("id") or "",
            "suite": suite,
            "lang": case.get("lang") or result.get("lang") or "",
            # The spine axes, passed through when the suite carries them. partner_p0.jsonl does
            # not, and an empty string is the honest value — inventing a stratum here would put a
            # fabricated label on the very axis the grid slices by.
            #
            # `mechanism` is READ AS scam_strata, and only that one. generated_v1.jsonl carries the
            # field under the older name and its seven values were checked against
            # data/seeds/mechanism_ontology.json: all seven are ontology mechanism codes, i.e. the
            # same vocabulary the strata axis uses, not a parallel taxonomy. Renaming a field is
            # not inventing a label. No such equivalence exists for the other three axes, so they
            # stay blank rather than being guessed from the mechanism.
            "scam_strata": case.get("scam_strata") or case.get("mechanism") or "",
            "user_strata": case.get("user_strata") or "",
            "scam_cluster": case.get("scam_cluster") or "",
            "user_cluster": case.get("user_cluster") or "",
            "archetype": case.get("archetype") or "",
            # QS-11: `exposure` was ALWAYS the empty string, on every row this harness has ever
            # written, because no suite carries the field — so the dashboard's exposure axis existed
            # as a column and never as data. That is the axis the client's whole v3/v4 complaint runs
            # along: a no-loss enquiry handed recovery rails. A pass rate that cannot be split by it
            # cannot show the defect. Derived from the USER'S OWN WORDS when the case does not declare
            # it (see exposure_from_turns), and the derivation is deterministic and stated rather than
            # guessed from the mechanism — which is what the note below correctly refuses to do.
            "exposure": case.get("exposure") or exposure_from_turns(case),
            # QS-08/QS-11: where this conversation came from. `mined_real.jsonl` called all 400 of its
            # cases "production" while 391 were replays of Test-API simulation contacts, so a
            # dashboard cell labelled production was reporting on generated surface. Carried per row
            # so the split is visible rather than assumed.
            "provenance": (case.get("source") or "authored").strip().lower(),
            "affect": case.get("affect") or "",
            "saturation": case.get("saturation") or "",
            "certainty": case.get("certainty") or "",
            "turn_act": case.get("turn_act") or "",
            "continuity": case.get("continuity") or "",
            "user_text": turn.get("user") or "",
            "reply_text": "\n\n".join(replies),
            "bubble_count": len(replies),
            "required_actions": required,
            "forbidden_actions": forbidden,
            "delivered_actions": [],
            "passed": not turn_fails,
            "fails": turn_fails,
            "soft": soft,
            "latency_ms": float(turn.get("latency_ms") or 0),
            "judge_tokens": 0,
            "turn_owner": turn.get("turn_owner") or "",
            "judge_verdict": "", "judge_reason": "",
            **usage_columns(turn.get("usage") or {}, case_persona),
            # There is deliberately no LLM judge in this harness (see the module docstring), so
            # every row it writes is deterministically graded and says so. judge_vs_deterministic()
            # reads the empty judge_verdict and reports these turns as unjudged rather than as
            # agreement, which is the difference between "the judge agreed" and "no judge ran".
            "grader": "deterministic",
        })
    return rows


def olap_start(run_id, args, case_count):
    """Post the run as status='running' before the first case, and return the client (or None).

    The run identity is fixed here, up front, so every later write targets the same key and a run
    that dies half way leaves a row that says 'running' — which is information. A missing row is
    indistinguishable from a run that was never started.
    """
    if not olap_enabled():
        print("  !! OLAP disabled: this run will NOT be traceable in DuckDB — "
              "only use for throwaway unit checks, never for a real evaluation.")
        return None
    if OlapClient is None:
        # Asked for, and unavailable. Loud, and distinguished from "disabled": the run continues
        # (a scorecard is still worth having) but it must not be mistaken for a traceable one.
        print(f"  !! OLAP REQUESTED BUT UNAVAILABLE ({OLAP_IMPORT_ERROR}) — this run will NOT be "
              f"traceable in DuckDB. Run it with services/intelligence/.venv/bin/python, or "
              f"`pip install httpx`.", file=sys.stderr)
        return None
    client = OlapClient(enabled=True)
    client.post_run({
        "run_id": run_id, "suite": SUITE_NAME, "harness": "conversation",
        "label": args.label, "git_sha": git_sha(),
        "started_at": _now_iso(), "status": "running",
        "total": case_count,
        "progress": {"cases_total": case_count, "cases_done": 0, "suite_path": args.suite,
                     "base": BASE, "repeat": args.repeat},
        "metadata": {"suite_path": args.suite, "base": BASE, "repeat": args.repeat},
    })
    return client


def olap_heartbeat(client, run_id, started_at, args, *, done, total, passed, errors):
    """Move the run row's counters while the run is still going.

    The turns were already streaming case by case, but the run ROW was written once at the start
    with `cases_done: 0` and not touched again until the end, so the dashboard read "0 of 416" for
    the entire multi-hour run and then jumped to complete. A progress field that only becomes true
    when the work is finished is not progress, it is a result with a misleading name.

    HARNESS ERRORS ARE CARRIED SEPARATELY from failures, all the way through. A timeout or a dropped
    connection is the measuring instrument breaking, not the product failing, and a run reporting a
    clean pass rate because the harness died is the worst reading this system can produce.

    It never raises. `OlapClient._post` already retries and then spools, and a telemetry write must
    not be able to end an eval that is otherwise working.
    """
    if client is None:
        return
    client.post_run({
        "run_id": run_id, "suite": SUITE_NAME, "harness": "conversation",
        "label": args.label, "git_sha": git_sha(),
        "started_at": started_at, "status": "running",
        "duration_s": round(time.time() - _EPOCH[0], 1),
        "total": total, "passed": passed, "harness_errors": errors,
        "progress": {"cases_total": total, "cases_done": done, "suite_path": args.suite,
                     "base": BASE, "repeat": args.repeat},
    })


def olap_finish(client, run_id, report, cases_by_id, args, started_at, posted=()):
    """Post any remaining turns and the final run row (status='done')."""
    if client is None:
        return
    already = set(posted)
    total_turns = 0
    for result in report.get("results", []):
        case = cases_by_id.get(result.get("id")) or {}
        rows = turn_rows(case, result)
        total_turns += len(rows)
        if rows and result.get("id") not in already:
            client.post_turns(run_id, rows, suite=SUITE_NAME)
    passed = sum(1 for r in report.get("results", []) if r.get("passed"))
    total = len([r for r in report.get("results", []) if not r.get("harness_error")])
    usage = run_usage_totals(report)
    run_row = {
        "run_id": run_id, "suite": SUITE_NAME, "harness": "conversation",
        "label": args.label, "git_sha": git_sha(),
        "started_at": started_at, "duration_s": round(time.time() - _EPOCH[0], 1),
        "status": "done",
        "total": total, "passed": passed, "failed": max(total - passed, 0),
        "total_turns": total_turns,
        "p0_total": int(report.get("p0_total") or 0),
        "p0_passed": int(report.get("p0_passed") or 0),
        "flaky_cases": list(report.get("flaky_cases") or []),
        "harness_errors": int(report.get("cases_harness_errors") or 0),
        "progress": {"cases_total": report.get("cases_total"), "cases_done": report.get("cases_ran")},
        "metadata": {"fail_modes": report.get("fail_modes", {}),
                     "soft_modes": report.get("soft_modes", {}),
                     "base": BASE, "repeat": report.get("repeat", 1),
                     "suite_path": args.suite,
                     # The instrument's own status, recorded IN THE RUN rather than only in a
                     # docstring: a reader looking at a token column has to be able to tell an
                     # absent instrument from a measured zero without leaving the row.
                     "usage": {"turns_captured": usage["turns_captured"],
                               "turns_uncaptured": usage["turns_uncaptured"],
                               "note": ("per-turn usage came from the Test API usage envelope"
                                        if usage["turns_captured"] else
                                        "NO turn reported usage — the app binary predates the usage "
                                        "envelope, or every turn failed before reporting. Token "
                                        "columns are NULL and must not be read as zero-LLM turns")}},
    }
    # Only declare run-level token totals we actually measured. A run whose turns went unmeasured
    # leaves these columns unset, so eval_token_summary reports "declared" as absent rather than as
    # a zero bill for a run that certainly cost something.
    for key in ("gen_prompt_tokens", "gen_completion_tokens", "llm_calls", "zero_llm_turns"):
        if usage.get(key) is not None:
            run_row[key] = usage[key]
    client.post_run(run_row)
    # Measure this corpus against real traffic. Only the service can see the traces table, so it
    # computes both sides; a harness-side "real traffic" number would be a guess.
    client.post_alignment(run_id, compute=True)
    print(f"OLAP: run {run_id} recorded ({total_turns} turns)")


_EPOCH = [time.time()]


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def reclassify_throttled_cases(results, cases_by_id, max_retries=6, cooldown_s=None):
    """A 429 IS NOT A VERDICT. Re-run PYTHON_PIPELINE_ERROR cases alone and label what they really were.

    Round 3 §3: both suites reported PYTHON_PIPELINE_ERROR on 2 cases each, the author checked before
    reporting a regression, and it was upstream rate-limiting under sustained load — the same cases ran
    clean in isolation. That check was correct and it was MANUAL. Doing it by hand is not a control:
    the next person sees a red gate, and a red gate that is sometimes infrastructure is a red gate
    people learn to re-run until it is green, which is the same thing as not having one.

    So the harness does what the author did. `go_fallback:python_error` (internal/handlers/
    turn_entry.go:266) means Go called Python, Python threw, and Go answered instead — the reply the
    user got was the FALLBACK, so nothing scored against that turn is a measurement of the product.
    That is true whether the throw was a throttle or a bug, which is why the retry adopts the clean
    run's result rather than only clearing one label.

    The two outcomes are DIFFERENT LABELS, which is the point — today they share a fail mode and a
    real defect and a throttle look identical:

      reproduced   -> PYTHON_PIPELINE_ERROR(QS-12) stands. Hard fail, unchanged. A genuine pipeline
                      error fails the gate exactly as it does now.
      not reproduced -> the isolation run's verdict replaces it, with a soft UPSTREAM_THROTTLED note
                      and the original verdict kept alongside it in the report.

    Nothing else is retried. A case failing on content is not re-rolled until it passes; only the one
    fail mode that means "the product never answered this turn" is, and only once.
    """
    suspect = [r for r in results
               if any(str(f).startswith("PYTHON_PIPELINE_ERROR") for f in (r.get("fails") or []))]
    if not suspect:
        return {"suspect": 0, "retried": 0, "throttled": [], "reproduced": [], "skipped": 0}
    # Corroboration, not proof. Read once for the whole run so the cost is one docker call, and
    # reported either way — "no rate-limit line in the log" next to "did not reproduce" is itself
    # information, and pretending the absence settles it would be the mirror of the mistake this
    # whole function exists to prevent.
    # The per-case lines above went to STDOUT and this narration goes to STDERR; without a flush the
    # two arrive in the wrong order under a pipe, and a reader sees "MINED-x FAIL" printed after
    # "MINED-x did NOT reproduce". The verdict in the report is correct either way, but a log that
    # reads backwards is a log that gets quoted backwards.
    sys.stdout.flush()
    log_line = container_saw_rate_limit(_wall_since_start() + 60)
    print(f"\nPACING: {len(suspect)} case(s) reported PYTHON_PIPELINE_ERROR. A 429 is not a verdict; "
          f"re-running each ALONE to tell a throttle from a defect.", file=sys.stderr)
    print(f"PACING: intelligence container rate-limit evidence: "
          f"{log_line or '(none found in the window)'}", file=sys.stderr)
    out = {"suspect": len(suspect), "retried": 0, "throttled": [], "reproduced": [],
           "skipped": max(0, len(suspect) - max_retries), "container_log": log_line}
    cool = COOLDOWN_S if cooldown_s is None else cooldown_s
    for r in suspect[:max_retries]:
        c = cases_by_id.get(r["id"])
        if not c:
            continue
        time.sleep(cool)                     # let the upstream window clear before asking again
        try:
            res2 = run_case(c)
        except HarnessError as e:
            print(f"  {r['id']:8} retry could not run ({e}) — original verdict stands", file=sys.stderr)
            continue
        out["retried"] += 1
        owners2 = [t.get("turn_owner") or "" for t in res2.get("turns") or []]
        if any(o.startswith("go_fallback:python_error") for o in owners2):
            out["reproduced"].append(r["id"])
            r["pipeline_error_reproduced"] = True
            # THE LABEL ITSELF SAYS IT REPRODUCED. `fail_modes` is the first thing anyone reads, and
            # "PYTHON_PIPELINE_ERROR: 2" there is exactly the line that cost round 3 an hour. No digits
            # in the suffix, so fail_turn_index() cannot mistake it for a turn index.
            r["fails"] = [f + "[reproduced-in-isolation]"
                          if str(f).startswith("PYTHON_PIPELINE_ERROR") else f
                          for f in (r.get("fails") or [])]
            print(f"  {r['id']:8} REPRODUCED in isolation — a genuine pipeline error, not a throttle",
                  file=sys.stderr)
            continue
        f2, s2 = score(c, res2)
        out["throttled"].append(r["id"])
        print(f"  {r['id']:8} did NOT reproduce in isolation — UPSTREAM_THROTTLED. "
              f"was: {','.join(r.get('fails') or []) or 'pass'} -> now: {','.join(f2) or 'pass'}",
              file=sys.stderr)
        # THE ORIGINAL VERDICT IS KEPT, NOT ERASED. A retry that leaves no trace is indistinguishable
        # from a suite that quietly re-rolls until it is green, which is the eval-side twin of editing
        # a gold. Both verdicts are in the report; only the isolation one counts.
        r["throttled_retry"] = {
            "original_fails": list(r.get("fails") or []),
            "original_passed": bool(r.get("passed")),
            "original_turn_owners": [t.get("turn_owner") or "" for t in r.get("turns") or []],
            "container_rate_limit_log": log_line,
            "reproduced": False,
        }
        r["fails"], r["soft"] = f2, list(s2) + ["UPSTREAM_THROTTLED(R3-3)"]
        r["passed"] = not f2
        r["turns"] = res2["turns"]
        r["session_id"] = res2["session_id"]
    return out


def _wall_since_start() -> float:
    return max(time.time() - (_EPOCH[0] or time.time()), 1.0)


def main():
    if not KEY:
        print("Set TEST_API_KEY to the test API key. It is not stored in this repository.",
              file=sys.stderr)
        sys.exit(2)
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="eval/suites/partner_p0.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--baseline", default="",
                    help="a previous report to compare against; a case that passed there and fails "
                         "now is a REGRESSION and exits non-zero.")
    ap.add_argument("--fail-on-regression", action="store_true",
                    help="exit 1 on any regression against --baseline, or on any P0 failure when no "
                         "baseline is given. Without this the harness exits 0 whatever it scored.")
    ap.add_argument("--freeze", default="",
                    help="write the current per-case verdicts to this path as a green set.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="run every case N times and score WORST-CASE; N>1 also reports flake rate. "
                         "Use this before believing a green result on an LLM-backed path.")
    # ---- pacing (round 3 §3) ----
    ap.add_argument("--no-lock", action="store_true",
                    help="do NOT take the exclusive suite lock. Only for a deliberate concurrent run; "
                         "concurrency is what manufactured round 3's PYTHON_PIPELINE_ERRORs.")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_S,
                    help="seconds to pace after the previous run before starting (default %(default)s).")
    ap.add_argument("--no-throttle-retry", action="store_true",
                    help="do NOT re-run PYTHON_PIPELINE_ERROR cases in isolation. The retry is what "
                         "separates an upstream 429 from a real pipeline error; turning it off makes "
                         "them indistinguishable again.")
    a = ap.parse_args()
    with SuiteLock(enabled=not a.no_lock, cooldown_s=a.cooldown) as lock:
        return _run(a, lock)


def _run(a, lock):
    global SUITE_NAME
    SUITE_NAME = suite_name_from_path(a.suite)

    # Before anything is scored: prove the host on the other end is this product. Exits 2 if not.
    assert_target_is_our_build()

    cases = [json.loads(l) for l in open(a.suite, encoding="utf-8") if l.strip()]
    if a.only:
        cases = [c for c in cases if c["id"] in a.only.split(",")]
    cases_by_id = {c["id"]: c for c in cases}
    # Readable, sortable, and still unique. A bare uuid4 is a correct key and a useless label: the
    # dashboard's run picker shows the id, and "conv-media_v1-20260801T101500Z-4f2a" tells a reader
    # which suite ran and when, which "9c1e…" does not. The short uuid tail keeps two runs of the
    # same suite in the same second from upserting onto each other.
    run_id = f"conv-{SUITE_NAME}-{_now_iso().replace('-', '').replace(':', '')}-{uuid.uuid4().hex[:4]}"
    _EPOCH[0] = time.time()
    started_at = _now_iso()
    olap = olap_start(run_id, a, len(cases))
    if a.repeat > 1:
        results = run_repeated(cases, a.repeat)
        for r in results:
            flag = "FLAKE" if r.get("flake") else ("PASS" if r["passed"] else "FAIL")
            print(f"  {r['id']:8} {flag}  {','.join(r['fails'])}"
                  + (f"   [{r.get('failed_runs', 0)}/{r.get('runs', 0)} runs failed]"
                     if r.get("runs") else ""))
        p0 = [r for r in results if r["pri"] == "P0"]
        flakes = [r["id"] for r in results if r.get("flake")]
        rep = {
            # run_id rides in the report so the JUDGE can attach its verdicts to the same DuckDB run
            # without the caller having to copy an id off a log line. Additive: every existing key
            # keeps its meaning and nothing reads a missing one.
            "label": a.label, "base": BASE, "repeat": a.repeat, "run_id": run_id,
            # WHICH SUITE THIS IS. Until 2026-08-04 the only record of it was the OUTPUT FILENAME the
            # caller happened to pass, so any grader reading eval/reports/ had to parse `POST-v2_feedback.json`
            # and hope `v2_feedback_extra` never got mistaken for `v2_feedback`. Additive; nothing reads
            # a missing one (verbiage_report.suite_of falls back to the filename for older reports).
            "suite": SUITE_NAME,
            "cases_total": len(cases), "cases_ran": len(results), "cases_harness_errors": 0,
            "p0_total": len(p0), "p0_passed": sum(1 for r in p0 if r.get("passed")),
            "p0_pass_pct": round(100 * sum(1 for r in p0 if r.get("passed")) / max(len(p0), 1), 1),
            "flaky_cases": flakes, "fail_modes": {}, "soft_modes": {}, "results": results,
        }
        for r in results:
            for f in r.get("fails", []):
                rep["fail_modes"][f] = rep["fail_modes"].get(f, 0) + 1
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        json.dump(rep, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"\nP0 {rep['p0_passed']}/{rep['p0_total']} = {rep['p0_pass_pct']}%  "
              f"(worst of {a.repeat} runs)")
        if flakes:
            # Loud on purpose. An intermittent P0 certifies itself clean some fraction of the time,
            # which is worse than a consistent failure: it teaches you to trust a number that lies.
            print(f"*** {len(flakes)} INTERMITTENT CASE(S) — real defects that pass some runs: "
                  f"{', '.join(flakes)} ***")
        print("fail modes:", json.dumps(rep["fail_modes"], indent=1))
        print("->", a.out)
        # The repeat path posts turns at the END, not as cases complete: a case's verdict here is
        # WORST-OF-N and is not knowable until its last repetition has run. Posting the first
        # repetition's turns would publish a verdict the run then contradicts.
        olap_finish(olap, run_id, rep, cases_by_id, a, started_at)
        return gate(rep, a)

    # Case ids whose turns are already in DuckDB. Tracked HERE rather than as a key on the result
    # dict, so the JSON report shape stays exactly what it was — a harness must not change the
    # artefact it produces in order to record itself.
    posted = set()
    results, ran, errored = [], 0, 0
    for c in cases:
        try:
            res = run_case(c)
        except HarnessError as e:                   # infra: excluded from the denominator, LOUDLY
            errored += 1
            print(f"  {c['id']:8} HARNESS-ERROR (excluded) {e}", file=sys.stderr)
            results.append({"id": c["id"], "pri": c["pri"], "partner": c.get("partner", ""),
                            "error": str(e), "fails": [], "soft": [], "harness_error": True,
                            "passed": None})
            continue
        except Exception as e:                      # a case that cannot run is NOT a pass (F25d)
            errored += 1
            print(f"  {c['id']:8} ERROR {e}", file=sys.stderr)
            results.append({"id": c["id"], "pri": c["pri"], "partner": c.get("partner", ""),
                            "error": str(e), "fails": ["HARNESS_ERROR"], "soft": [], "passed": False})
            continue
        ran += 1
        f, s = score(c, res)
        res["fails"], res["soft"], res["passed"] = f, s, not f
        results.append(res)
        print(f"  {c['id']:8} {'PASS' if not f else 'FAIL'}  {','.join(f)}"
              + (f"   [soft: {','.join(s)}]" if s else ""))
        # AS THE CASE COMPLETES, not at the end: a run that dies on case 20 of 36 has still written
        # the 19 it graded, and the run row left at status='running' says the rest never happened.
        if olap is not None:
            olap.post_turns(run_id, turn_rows(c, res), suite=SUITE_NAME)
            posted.add(c["id"])
            # AND THE COUNTER MOVES. The turns were already streaming, but `progress.cases_done`
            # was written once at 0 and not again until the run finished, so the dashboard showed
            # "0 of 416" for however many hours the run took. A progress field that only becomes
            # true at the end is not progress; it is a result with a misleading name.
            #
            # Every case rather than every N, because the run row is a single upsert against a
            # local DuckDB and a 2,000-turn run makes 416 of them. That is not the expensive part
            # of an eval that is making a model call per turn.
            olap_heartbeat(olap, run_id, started_at, a, done=ran, total=len(cases),
                           passed=sum(1 for r in results if r.get("passed")),
                           errors=sum(1 for r in results if r.get("harness_error")))

    # BEFORE the report is built, because the report is what the gate and the dashboard read. A case
    # whose turns were served by the Go fallback was never measured; re-running it alone is the only
    # way to know whether that was a throttle or a defect. See reclassify_throttled_cases().
    throttle = {"suspect": 0, "retried": 0, "throttled": [], "reproduced": [], "skipped": 0}
    if not a.no_throttle_retry:
        throttle = reclassify_throttled_cases(results, cases_by_id, cooldown_s=a.cooldown)
        for r in results:
            if r.get("id") in throttle["throttled"] and olap is not None and r["id"] in posted:
                olap.post_turns(run_id, turn_rows(cases_by_id[r["id"]], r), suite=SUITE_NAME)

    p0 = [r for r in results if r["pri"] == "P0" and not r.get("harness_error")]
    engines = sorted({str(t.get("turn_engine") or "") for r in results
                      for t in (r.get("turns") or [])})
    rep = {
        "label": a.label, "base": BASE, "run_id": run_id,
        # WHAT THIS RUN ACTUALLY MEASURED, recorded next to the host rather than assumed from it.
        # Thirteen of the fifteen frozen greensets in this repo record a base of 127.0.0.1:8100 and
        # nothing else, which is why nobody could tell they were baselines of the OLD product.
        "target": {
            "engine": TARGET.get("engine", ""),
            "engines_seen": engines,
            "ai": TARGET.get("ai", {}),
            "turn_machine_env": os.environ.get("BOXX_TURN_MACHINE", ""),
        },
        # See the note on the --repeat report above: the suite name rides in the JSON so a grader
        # never has to infer it from the output filename the caller chose.
        "suite": SUITE_NAME,
        "cases_total": len(cases), "cases_ran": ran, "cases_harness_errors": errored,
        "p0_total": len(p0), "p0_passed": sum(1 for r in p0 if r.get("passed")),
        "p0_pass_pct": round(100 * sum(1 for r in p0 if r.get("passed")) / max(len(p0), 1), 1),
        # R3 §3 — PACING, AS DATA. A run that was green after absorbing 40 429s and a run that was
        # green without touching the limiter are different facts about the stack, and only the JSON
        # can carry that to whoever reads the gate tomorrow.
        "pacing": {
            "lock_waited_s": getattr(lock, "waited_s", 0.0),
            "lock_cooldown_s": getattr(lock, "cooled_s", 0.0),
            "lock_note": getattr(lock, "note", ""),
            "http_429": THROTTLE["http_429"],
            "backoff_s": round(THROTTLE["backoff_s"], 1),
            "retries_exhausted": THROTTLE["exhausted"],
            **throttle,
        },
        "fail_modes": {}, "soft_modes": {},
        "results": results,
    }
    for r in results:
        for f in r.get("fails", []):
            rep["fail_modes"][f] = rep["fail_modes"].get(f, 0) + 1
        for s in r.get("soft", []):
            rep["soft_modes"][s] = rep["soft_modes"].get(s, 0) + 1
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(rep, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    _assert_attributable(rep)
    print(f"\nP0 {rep['p0_passed']}/{rep['p0_total']} = {rep['p0_pass_pct']}%"
          + (f"  *** {errored} CASES EXCLUDED (harness error) — RESULT IS INCOMPLETE ***" if errored else ""))
    print("fail modes:", json.dumps(rep["fail_modes"], indent=1))
    # Never silent, even when it changed nothing: "0 429s, 0 pipeline errors" is the line that lets a
    # reader believe the number above it.
    pc = rep["pacing"]
    print(f"pacing: {pc['http_429']} HTTP 429 absorbed ({pc['backoff_s']}s backoff), "
          f"{pc['suspect']} pipeline-error case(s), {len(pc['throttled'])} throttled "
          f"{'(' + ','.join(pc['throttled']) + ') ' if pc['throttled'] else ''}"
          f"/ {len(pc['reproduced'])} genuine"
          f"{' (' + ','.join(pc['reproduced']) + ')' if pc['reproduced'] else ''}"
          + (f", lock waited {pc['lock_waited_s']}s" if pc['lock_waited_s'] else ""))
    print("->", a.out)
    olap_finish(olap, run_id, rep, cases_by_id, a, started_at, posted)
    return gate(rep, a)


def _assert_attributable(rep):
    """Refuse to certify a run nobody can attribute to an engine. Writes the report first.

    NOT "every turn must name an engine". A turn can legitimately end above stage 32 — the
    disclaimer on first contact, a button handled by a flow, a PII refusal — and those genuinely
    had no engine. Demanding one everywhere would refuse valid runs, and a fence that cries wolf
    gets switched off, which is how the host fence came to be bypassable in the first place.

    Two things are refused, and both mean the same thing: this run is not one measurement.

    * **No turn named an engine.** Either nothing reached the turn engine at all, or the host is a
      build that does not report one. Scoring it would produce a number about an unknown product.
    * **Both engines answered inside one run.** ``BOXX_TURN_MACHINE`` is read per turn, so a restart
      or an env edit mid-suite splits the suite across two products. Half a machine measurement and
      half a legacy one averages to a fact about neither.
    """
    seen = [e for e in rep["target"]["engines_seen"] if e]
    if len(seen) == 1:
        return
    print("=" * 100, file=sys.stderr)
    print("REFUSING TO CERTIFY: this run cannot be attributed to one engine.", file=sys.stderr)
    if not seen:
        print("  No turn reported a turn_engine. Either nothing reached the turn engine, or the",
              file=sys.stderr)
        print("  host does not report one — in which case it is not this build.", file=sys.stderr)
    else:
        print(f"  Two engines answered inside one suite: {seen}. BOXX_TURN_MACHINE is read per",
              file=sys.stderr)
        print("  turn, so the flag moved mid-run and this is two half-measurements.",
              file=sys.stderr)
    print(f"  base={rep['base']}  BOXX_TURN_MACHINE={rep['target']['turn_machine_env']!r}",
          file=sys.stderr)
    print(f"  The report was still written to disk for diagnosis: {rep.get('run_id', '')}",
          file=sys.stderr)
    print("=" * 100, file=sys.stderr)
    sys.exit(2)


def gate(rep, a):
    """Turn a report into an exit code.

    Audit F31: eval/greenset/ held only a .gitkeep and green_gate.py's make target was documented
    DEFERRED — which is why commit fa7ccc1 exists, a REVERT of a language fix that broke the opposite
    case. A frozen green set is precisely the mechanism that prevents that, and it was never armed.
    This harness had the same hole from the other end: main() had no sys.exit at all, so it returned 0
    whether it scored 36/36 or 0/36. A gate that cannot fail is not a gate; it is a report.

    Regression is defined against a BASELINE, not against perfection. A case that has never passed is a
    known gap and must not block work on something else; a case that passed yesterday and fails today is
    a regression and must. That distinction is the whole point — "0 regressions, toward-expected only".
    """
    if a.freeze:
        os.makedirs(os.path.dirname(a.freeze) or ".", exist_ok=True)
        frozen = {r["id"]: bool(r.get("passed")) for r in rep["results"]
                  if not r.get("harness_error")}
        json.dump({"suite": a.suite, "base": BASE, "verdicts": frozen},
                  open(a.freeze, "w", encoding="utf-8"), indent=1, sort_keys=True)
        print(f"froze {sum(frozen.values())}/{len(frozen)} passing verdicts -> {a.freeze}")

    if not a.fail_on_regression:
        return 0

    if rep.get("cases_harness_errors"):
        # F25d, exactly: an incomplete run must never read as a pass.
        print("GATE: INCOMPLETE — harness errors excluded cases; refusing to certify", file=sys.stderr)
        return 2

    if a.baseline:
        try:
            base = json.load(open(a.baseline, encoding="utf-8"))
        except OSError as e:
            print(f"GATE: baseline unreadable ({e}); refusing to certify", file=sys.stderr)
            return 2
        was = base.get("verdicts") or {r["id"]: bool(r.get("passed")) for r in base.get("results", [])}
        regressions, fixed, standing = [], [], []
        for r in rep["results"]:
            now = bool(r.get("passed"))
            before = was.get(r["id"])
            if before is True and not now:
                regressions.append(f"{r['id']} ({','.join(r.get('fails', []))})")
            elif before is False and now:
                fixed.append(r["id"])
            elif before is False and not now:
                standing.append(f"{r['id']} ({','.join(r.get('fails', []))})")
        # R6 — NAME THE CASE AND THE MODE, ALWAYS, NOT ONLY ON A REGRESSION.
        #
        # mined_production is 9 conversations, so one case flipping moves the headline 11 points and
        # a fraction on its own is not diagnosable. It is also the only real traffic in the repo and
        # the only suite that found any of the revalidation's §2.4-§2.6 defects, which is why it is
        # in the gate at all. A green "0 regressions" over six standing failures is true and useless;
        # printing the standing set with its modes is what makes a 3/9 legible as WHICH three.
        if standing:
            print(f"GATE: {len(standing)} standing failure(s) (red in the baseline too, not a "
                  f"regression): " + "; ".join(standing))
        if fixed:
            print(f"GATE: {len(fixed)} newly passing: {', '.join(fixed)}")
        if regressions:
            print(f"GATE: FAIL — {len(regressions)} REGRESSION(S): " + "; ".join(regressions),
                  file=sys.stderr)
            return 1
        print("GATE: PASS — 0 regressions against the baseline")
        return 0

    # No baseline: every P0 must pass. Intentionally strict, so a suite used this way cannot drift.
    failed = [r["id"] for r in rep["results"] if r["pri"] == "P0" and not r.get("passed")]
    if failed:
        print(f"GATE: FAIL — {len(failed)} P0 case(s) failing: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("GATE: PASS — all P0 cases passing")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
