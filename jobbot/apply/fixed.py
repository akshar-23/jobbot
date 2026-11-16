"""Answers that never come from AI.

Each question is checked against these rules before anything goes to Claude:
- contact details, links, work authorization, sponsorship, citizenship, salary,
  demographics, education, location and "worked here before" -> answers.toml
- questions about using AI, legal history, or agreements -> a Blocker (you
  decide), unless bot.toml allows accepting required agreements
- anything phrased in a way these rules don't recognize -> None, and the
  drafter handles it from facts.md

When a rule isn't sure (an odd negation in a sponsorship question, options that
don't start with Yes/No), it returns a Blocker rather than guessing.

Your saved answers (profile/saved_answers.json, jobbot/saved.py) come right after the
AI-policy, applied-before and agreement checks: an answer you typed yourself beats a rule.
"""

from __future__ import annotations

import re
from datetime import date

from jobbot import profile as person
from jobbot import saved
from jobbot.apply.model import Answer, Blocker, Question

DECLINE_RE = re.compile(r"decline|don.?t wish|do not wish|don.?t want to answer|do not want to answer|prefer not"
                        r"|not to (answer|disclose|say)|choose not|rather not", re.I)

# Checked in this order; the first match decides.
AI_POLICY_RE = re.compile(
    r"ai policy|use of ai|using ai|\bai[- ]generated|ai (tools?|assistants?|systems?) (to|in|for|during|when|while)"
    r"|without (the )?(use|help|assistance) of (ai|artificial)|artificial intelligence (tools?|assist)"
    r"|chatgpt|generative ai (to|in|for|during)", re.I)
LEGAL_RE = re.compile(
    r"felony|convicted|conviction|criminal|background check|drug (test|screen)|non-?compete"
    r"|restrictive covenant|non-?solicit|export control|\bitar\b|deemed export", re.I)
MARKETING_RE = re.compile(r"whatsapp|\bsms\b|text messag|newsletter|marketing (email|messag|communica)"
                          r"|share (my|your) (resume|information|contact|profile|data|application)", re.I)
PHONE_TYPE_RE = re.compile(r"phone (device )?type|type of phone", re.I)
PHONE_CODE_RE = re.compile(r"(country )?phone code|country code|dialing code", re.I)
ADDRESS_LINE_RE = re.compile(r"address line ?(1|2)|street address|^address\b", re.I)
PLAIN_CITY_RE = re.compile(r"^(city|town|city / town)$", re.I)
PLAIN_STATE_RE = re.compile(r"^(state|state / province|state/province|province|region)$", re.I)
POSTAL_RE = re.compile(r"postal code|zip( code)?|postcode", re.I)
RESIDENT_RE = re.compile(r"\bresident of (the state of )?([a-z][a-z .]+?)\s*\?", re.I)
FUTURE_ROLES_RE = re.compile(r"future (job|role|opportunit|opening|position)|talent (community|network|pool)"
                             r"|other (roles|positions|opportunities|openings)", re.I)
CONSENT_RE = re.compile(
    r"arbitrat|\bi agree\b|do you agree|agree to|acknowledg|\bconsent\b|\bi certify\b|certify that|\battest"
    r"|privacy (notice|policy)|terms (of|and) |read (and|&) (understand|agree|accept)|e-?signature|\bsignature\b", re.I)
ARBITRATION_RE = re.compile(r"arbitrat|class action|jury trial|waive", re.I)
# Only acknowledging that you've read a privacy notice counts here. Consents to
# recording, AI notetakers or data sharing are "other agreements".
PRIVACY_RE = re.compile(r"(acknowledg|read|review|received?)\w*[^.?]{0,80}(privacy (notice|policy|statement)|"
                        r"applicant (privacy|data)|candidate privacy|personal (data|information) (notice|policy))", re.I)
OTHER_CONSENT_RE = re.compile(r"record|notetak|transcri|ai\b|artificial intelligence", re.I)
REMOTE_INTENT_RE = re.compile(r"(plan|intend|want|prefer|like|choose) to work remote|work (from a |in a )?remote(ly| location)? if"
                              r"|do you (plan|intend) to work (from a )?remote", re.I)
SPONSOR_NEED_RE = re.compile(r"(require|need)s?\b[^?]{0,80}sponsor|sponsorship[^?]{0,40}(now or|in the future)", re.I)
FULL_NAME_RE = re.compile(r"^(full |legal |full legal )?name$|^your (full )?name$|full legal name|^legal name", re.I)

AUTH_RE = re.compile(r"authori[sz]ed to work|legally (authori[sz]ed|eligible|able|permitted) to work|eligible to work"
                     r"|right to work|work authori[sz]ation\b(?! status)", re.I)
STATUS_RE = re.compile(r"(visa|immigration|work authori[sz]ation|citizenship) status|type of (visa|work authori)"
                       r"|current (visa|work) (status|type)", re.I)
SPONSOR_RE = re.compile(r"sponsor|\bvisa\b|work permit|immigration|h-?1b|employment[- ]based", re.I)
NEGATION_RE = re.compile(r"\bwithout\b|\bnot require\b|\bno longer\b|\bdon.?t need\b|\bdo not need\b|\bunable\b", re.I)
CITIZEN_RE = re.compile(r"\bu\.?s\.? citizen|citizenship\b|permanent resident|green card", re.I)
US_PERSON_RE = re.compile(r"u\.?s\.? person", re.I)
EMBARGOED_RE = re.compile(r"cuba|iran|north korea|syria|crimea", re.I)
CITIZENSHIP_COUNTRY_RE = re.compile(r"countr(y|ies) of (your )?citizenship|citizenship countr|nationality"
                                    r"|what (is|are) your (current )?citizenship|citizen of which", re.I)
CLEARANCE_RE = re.compile(r"clearance", re.I)
AGE_RE = re.compile(r"18 years|at least 18|over 18|of legal (working )?age", re.I)

COUNTRY_RE = re.compile(r"country (where|of|in which) you (currently )?(reside|live)|country of (residence|citizenship)?$"
                        r"|^country\b|which country", re.I)
LOCATION_TEXT_RE = re.compile(r"city and state|where (are you|do you) (currently )?(located|based|live|reside)"
                              r"|current (city|location)|^location\b|^city\b|address from which|where will you be (working|based)"
                              r"|what city", re.I)
STATE_RE = re.compile(r"state (of residence|you (currently )?(reside|live) in|where you (live|reside))|which state", re.I)
RELOCATE_RE = re.compile(r"relocat", re.I)
START_RE = re.compile(r"start date|earliest .*(start|begin)|when (can|could|would) you (start|begin)|available to start"
                      r"|notice period|availability to start", re.I)
SALARY_RE = re.compile(r"salary|compensation|pay (expectation|range)|desired (pay|salary)|expected (pay|salary|compensation)", re.I)
WORKED_HERE_RE = re.compile(r"(ever|previously|before) (been )?(employed|worked) (by|for|at)|former (employee|intern)"
                            r"|currently (employed|working) (by|for|at)|worked (for|at) .* (before|previously)", re.I)
# "Please select if you were a previous Unity employee", "Are you an ex-employee?"
PREVIOUS_EMPLOYEE_RE = re.compile(r"(previous|former|past|ex-?)\s*(\{company\}|\w+\s?){0,3}\s*employee|current or (former|past) employee", re.I)
RELATIVE_RE = re.compile(r"\brelatives?\b|related to (anyone|any|an?|someone)|family members?\b[^?]{0,40}(work|employ)", re.I)
GOVERNMENT_RE = re.compile(r"(employed|worked) (by|for) (the |a |any )?(\w+ )?government|government (employee|employment|official|entity|agency)"
                           r"|public official|federal employee", re.I)
ACCOMMODATION_RE = re.compile(r"(require|need)\w*[^?]{0,60}accommodation", re.I)
ESSENTIAL_RE = re.compile(r"essential (functions|duties)|perform the (duties|functions) of (this|the) (job|role|position)", re.I)
# In-office / hybrid / onsite willingness. "Do you live within commuting distance" asks where
# you live now, not what you're willing to do, so it isn't matched here.
ONSITE_RE = re.compile(r"\b(on-?site|in[- ]office|in the office|from (the|our) office|hybrid|in[- ]person|days (a|per) week"
                       r"|commute|report to (the|our|an) office)\b", re.I)
LIVE_NOW_RE = re.compile(r"(live|reside|located|based) (with)?in|commuting distance|currently (live|reside)", re.I)
# Visa you hold or held ("Do you currently hold or have you held an H-1B?") is a status
# question, not a sponsorship one.
VISA_NAME_RE = re.compile(r"h-?1-?b|f-?1\b|stem[- ]opt|\bopt\b|\bcpt\b|\btn\b|l-?1\b|o-?1\b|e-?3\b|h-?4\b|j-?1\b", re.I)
VISA_HELD_RE = re.compile(r"\b(hold|held|currently (on|in|have)|are you (currently )?on|have you (ever )?(had|been on)|do you have an?)\b", re.I)
WORK_PERMIT_END_RE = re.compile(r"(opt|ead|work (permit|authori[sz]ation)|visa)\b[^?]{0,50}(expir|end date|valid (until|through))"
                                r"|(expir|end date)[^?]{0,40}\b(opt|ead|work (permit|authori[sz]ation)|visa)\b", re.I)
STEM_OPT_RE = re.compile(r"stem[- ]opt|stem extension", re.I)
INTERVIEWED_RE = re.compile(r"interviewed (with|at|for)", re.I)
APPLIED_BEFORE_RE = re.compile(r"applied (to|at|with|for) .* (before|previously|in the (past|last))|previously applied", re.I)
LANGUAGE_RE = re.compile(r"language skill|languages? (do you|you) speak|languages? spoken|language proficienc|which languages", re.I)
HEARD_RE = re.compile(r"how did you (first )?(hear|find|learn)|how you (first )?(heard|found|learned)"
                      r"|where did you (first )?(hear|find|see|learn)|source of (application|referral)"
                      r"|how were you referred|referral source", re.I)
REFERRED_RE = re.compile(r"were you referred|referred by|referral (name|from)|who referred|employee referral", re.I)
EMPLOYER_RE = re.compile(r"current (or (most recent|previous) )?(employer|company)|current or previous employer"
                         r"|most recent (employer|company)|who is your current", re.I)
JOB_TITLE_RE = re.compile(r"current (or (most recent|previous) )?(job )?title|current or previous job title"
                          r"|most recent (job )?title|what is your current (role|title)", re.I)
SCHOOL_RE = re.compile(r"^(school|university|college|institution)\b|which (school|university)", re.I)
DEGREE_RE = re.compile(r"^degree\b|highest (level of )?(education|degree)|degree (type|level|earned)", re.I)
MAJOR_RE = re.compile(r"discipline|major|field of study|area of study", re.I)
GRAD_RE = re.compile(r"graduat(ion|e) (date|year|month)|when did you graduate|year of graduation|expected graduation", re.I)
GPA_RE = re.compile(r"\bgpa\b|grade point", re.I)
GENDER_RE = re.compile(r"^gender|gender identity|\bsex\b", re.I)
RACE_RE = re.compile(r"\brace\b|ethnicit", re.I)
HISPANIC_RE = re.compile(r"hispanic|latin[oax]", re.I)
VETERAN_RE = re.compile(r"veteran", re.I)
DISABILITY_RE = re.compile(r"disabilit", re.I)
ORIENTATION_RE = re.compile(r"sexual orientation|lgbt|transgender", re.I)
PRONOUN_RE = re.compile(r"\bpronouns?\b", re.I)

LINK_RULES = [
    (re.compile(r"demo ?reel|show ?reel|\breel\b", re.I), "reel"),
    (re.compile(r"artstation", re.I), "artstation"),
    (re.compile(r"behance", re.I), "behance"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"^(other |additional )?links?$|^website or portfolio|^links? to your work", re.I), "portfolio"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"google scholar|scholar profile|publications? (link|url)", re.I), "google_scholar"),
    (re.compile(r"portfolio|personal (web)?site|^website\b|web ?site\b|personal url", re.I), "portfolio"),
]
NAME_RULES = [
    (re.compile(r"preferred (first )?name|nickname", re.I), "preferred_name"),
    (re.compile(r"^(legal )?first name|given name", re.I), "first_name"),
    (re.compile(r"^(legal )?last name|surname|family name", re.I), "last_name"),
    (re.compile(r"^e-?mail", re.I), "email"),
    (re.compile(r"phone|mobile number|cell", re.I), "phone"),
]
# Greenhouse's standard field ids.
KEY_RULES = {"first_name": "first_name", "last_name": "last_name", "email": "email",
             "phone": "phone", "preferred_name": "preferred_name"}
EEO_KEYS = {"gender": "gender", "race": "race_ethnicity", "hispanic_ethnicity": "hispanic_latino",
            "veteran_status": "veteran_status", "disability_status": "disability_status"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("*", "")).strip()


def match_option(options: list[str], wanted: str) -> str | None:
    """Exact, then prefix, then a unique substring match (case-insensitive)."""
    w = wanted.strip().lower()
    for o in options:
        if o.strip().lower() == w:
            return o
    starts = [o for o in options if o.strip().lower().startswith(w)]
    if len(starts) == 1:
        return starts[0]
    contains = [o for o in options if w in o.lower()]
    return contains[0] if len(contains) == 1 else None


def _yes_no(q: Question, value: bool, source: str) -> Answer | Blocker:
    word = "yes" if value else "no"
    if q.kind in ("text", "textarea"):
        return Answer(q.key, word.capitalize(), source)
    picks = [o for o in q.options if re.match(rf"\s*{word}\b", o, re.I)]
    if len(picks) == 1:
        return Answer(q.key, picks[0], source)
    return Blocker(q.key, q.label, f"expected one Yes/No option for '{word}', found {q.options[:6]}")


def _choose(q: Question, wanted: str, source: str) -> Answer | Blocker:
    # Search boxes (school) have no option list until you type; the form matches the text.
    if q.kind in ("text", "textarea") or not q.options:
        return Answer(q.key, wanted, source)
    hit = match_option(q.options, wanted)
    if hit:
        return Answer(q.key, hit, source)
    return Blocker(q.key, q.label, f"no option matches '{wanted}' in {q.options[:8]}")


def _decline_or(q: Question, value: str, source: str) -> Answer | Blocker:
    # Never fall through to the drafter: demographics and pronouns are not Claude's to guess.
    if not value:
        return Answer(q.key, "", "left blank (no answer set)") if not q.required else \
            Blocker(q.key, q.label, "required demographic question with no answer set")
    if DECLINE_RE.search(value) and q.options:
        picks = [o for o in q.options if DECLINE_RE.search(o)]
        if picks:
            return Answer(q.key, picks[0], source)
        return Answer(q.key, "", "left blank (no 'decline' option)") if not q.required else \
            Blocker(q.key, q.label, "no 'decline' option offered")
    return _choose(q, value, source)


# Demographic answers in answers.toml are meanings, not one form's wording: "No" for veteran
# finds "I am not a protected veteran", a race list picks the first entry a form offers.
EEO_NO = {"no", "false", "n", "none", "not a veteran", "not hispanic", "not hispanic or latino", "not disabled"}
EEO_NEGATIVE = {
    "hispanic_latino": re.compile(r"^\s*no\b|\bnot\b[^,.;]{0,20}hispanic|non-?hispanic", re.I),
    "veteran_status": re.compile(r"^\s*no\b|\bnot (a )?(protected )?veteran|non-?veteran|never served", re.I),
    "disability_status": re.compile(r"^\s*no\b|do(n.t| not) have a disability|not (have )?(a )?disab|no disability", re.I),
}
EEO_VETERAN_BUT_RE = re.compile(r"\bi am a (protected )?veteran\b|\bidentify as\b", re.I)
EEO_SYNONYMS = {"male": ["Male", "Man"], "female": ["Female", "Woman"], "man": ["Man", "Male"], "woman": ["Woman", "Female"]}


def _eeo(q: Question, field: str, value, source: str) -> Answer | Blocker:
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    prefs = [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]
    if not prefs or DECLINE_RE.search(prefs[0]):
        return _decline_or(q, prefs[0] if prefs else "", source)
    no = prefs[0].lower() in EEO_NO
    if q.kind in ("text", "textarea") or not q.options:
        return Answer(q.key, "No" if no else prefs[0], source)
    if field == "hispanic_latino" and any(re.search(r"\b(asian|white|black)\b", o, re.I) for o in q.options):
        return Blocker(q.key, q.label, "a race list under a Hispanic/Latino question; pick yourself")
    choices = [o for o in q.options if not DECLINE_RE.search(o)]
    if no and field in EEO_NEGATIVE:
        hits = [o for o in choices if EEO_NEGATIVE[field].search(o) and not EEO_VETERAN_BUT_RE.search(o)]
        if hits:
            return Answer(q.key, min(hits, key=len), source)
    else:
        for pref in [s for p in prefs for s in EEO_SYNONYMS.get(p.lower(), [p])]:
            low = pref.lower()
            rx = re.compile(rf"(?<![a-z]){re.escape(low)}(?![a-z])")
            hits = [o for o in choices if rx.search(o.lower())]
            if "indian" in low:
                hits = [o for o in hits if "american indian" not in o.lower()]
            if low == "asian":  # the generic answer mustn't pick one sub-region over another
                hits = [o for o in hits if not re.search(r"(south|east|southeast|central|west)[- ]?asian", o, re.I)]
            exact = [o for o in hits if o.strip().lower() == low]
            if exact or len(hits) == 1:
                return Answer(q.key, (exact or hits)[0], source)
            starts = [o for o in hits if o.strip().lower().startswith(low)]
            if len(starts) == 1:
                return Answer(q.key, starts[0], source)
    if not q.required:
        return Answer(q.key, "", "left blank (no option fits your answer)")
    return Blocker(q.key, q.label, f"no option fits your {field} answer {prefs[:3]}: {q.options[:8]}")


def _salary_option(q: Question, target: int) -> Answer | Blocker:
    for o in q.options:
        nums = [float(n.replace(",", "")) * (1000 if k else 1)
                for n, k in re.findall(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?", o) if n.replace(",", "")]
        nums = [n for n in nums if n >= 1000]
        if len(nums) >= 2 and min(nums) <= target <= max(nums):
            return Answer(q.key, o, "answers.toml: preferences.salary_target")
    return Blocker(q.key, q.label, "salary options don't include your target")


def degree_names(degree: str) -> list[str]:
    """How forms spell a degree: "Master of Science" -> "Master's Degree", "Master's", "MS", …"""
    d = (degree or "").lower()
    for word, names in (("doctor", ["Doctorate", "Ph.D.", "PhD", "Doctor of Philosophy"]),
                        ("ph", ["Doctorate", "Ph.D.", "PhD", "Doctor of Philosophy"]),
                        ("master", ["Master's Degree", "Master's", "Master of Science", "Masters", "MS"]),
                        ("bachelor", ["Bachelor's Degree", "Bachelor's", "Bachelor of Science", "Bachelors", "BS"]),
                        ("associate", ["Associate's Degree", "Associate's", "Associate Degree"])):
        if d.startswith(word):
            return ([degree] if degree not in names else []) + names
    return [degree] if degree else []


COUNTRY_ALIASES = {"united states": ["United States", "United States of America", "USA", "US", "U.S.", "U.S.A."]}


def _agreement(q: Question, label: str, cfg: dict) -> Answer | Blocker:
    """Required agreements: accepted only where bot.toml allows that kind."""
    # The "I have read" part is sometimes in the option, not the question.
    text = label + " " + " ".join(q.options)
    if ARBITRATION_RE.search(text):
        allowed, kind = cfg.get("accept_arbitration_agreements", False), "arbitration agreement"
    elif PRIVACY_RE.search(text) and not OTHER_CONSENT_RE.search(text):
        allowed, kind = cfg.get("accept_privacy_notices", False), "privacy notice"
    else:
        allowed, kind = cfg.get("accept_other_agreements", False), "agreement"
    if not allowed or q.kind in ("text", "textarea"):
        return Blocker(q.key, label, f"required {kind}; you decide (bot.toml)")
    picks = [o for o in q.options if re.match(r"\s*(yes|i agree|i acknowledge|i have read|i consent|i accept|agree|acknowledge|i confirm)", o, re.I)]
    return Answer(q.key, picks[0], f"bot.toml: accepts {kind}s") if picks else \
        Blocker(q.key, label, f"{kind} without an 'I agree' option")


def resolve(q: Question, answers: dict, company: str, cfg: dict) -> Answer | Blocker | None:
    """A fixed answer, a Blocker, or None (let the drafter answer from facts.md).

    cfg is bot.toml; it decides which required agreements may be accepted.
    """
    label = _norm(q.label)
    ident, links = answers.get("identity", {}), answers.get("links", {})
    auth, prefs = answers.get("work_authorization", {}), answers.get("preferences", {})
    edu, job = answers.get("education", {}), answers.get("current_job", {})
    eeo, misc = answers.get("voluntary_disclosures", {}), answers.get("misc", {})
    src = "answers.toml: "

    if q.kind == "hidden":
        return None

    # Manual-only and policy questions come first.
    if AI_POLICY_RE.search(label):
        return Blocker(q.key, label, "asks about using AI; the bot drafts with AI, so you answer this one")
    # Your own saved answers, except for agreements (bot.toml decides) and "applied here before".
    if not (CONSENT_RE.search(label) or APPLIED_BEFORE_RE.search(label)):
        mine = saved.lookup(q, company)
        if mine:
            return mine
    # Export-control "U.S. person" status follows from answers.toml: a U.S. person is a citizen,
    # permanent resident, refugee or asylee. Answered only when exactly one option fits.
    if US_PERSON_RE.search(label) and q.options:
        us_person = bool(auth.get("us_citizen")) or bool(auth.get("permanent_resident"))
        country = auth.get("citizenship", "")
        if us_person:
            picks = [o for o in q.options if re.search(r"\bi am a u\.?s\.? person\b", o, re.I) and not re.search(r"\bnot\b", o, re.I)]
        elif country and not EMBARGOED_RE.search(country):
            picks = [o for o in q.options if re.search(r"none of the above|different country|not a u\.?s\.? person", o, re.I)
                     and not EMBARGOED_RE.search(o)]
        else:
            picks = []
        if len(picks) == 1:
            return Answer(q.key, picks[0], src + "work_authorization (us_citizen, permanent_resident, citizenship)")
        return Blocker(q.key, label, "U.S.-person status question with options that don't map cleanly; answer yourself")

    # "Country of citizenship" (often worded as an export-control question): answered only
    # when answers.toml has it and the options are countries, never Yes/No variations.
    if CITIZENSHIP_COUNTRY_RE.search(label):
        country = auth.get("citizenship", "")
        if not country:
            return Blocker(q.key, label, "asks your country of citizenship (set work_authorization.citizenship to answer)")
        if q.options and any(re.match(r"\s*(yes|no)\b", o, re.I) for o in q.options):
            return Blocker(q.key, label, "citizenship question with Yes/No options; answer yourself")
        return _choose(q, country, src + "work_authorization.citizenship")
    if LEGAL_RE.search(label):
        return Blocker(q.key, label, "legal or background question")
    if MARKETING_RE.search(label):
        return _yes_no(q, bool(misc.get("marketing_opt_in", False)), src + "misc.marketing_opt_in")
    if FUTURE_ROLES_RE.search(label):
        return _yes_no(q, True, src + "misc (consider me for future roles)")
    if CONSENT_RE.search(label) and not PRONOUN_RE.search(label):
        if not q.required:
            return Answer(q.key, "", "left blank (optional agreement)")
        return _agreement(q, label, cfg)

    # Standard Greenhouse ids, then labels.
    if q.key in KEY_RULES:
        return Answer(q.key, str(ident.get(KEY_RULES[q.key], "")), src + f"identity.{KEY_RULES[q.key]}")
    if q.key in EEO_KEYS:
        return _eeo(q, EEO_KEYS[q.key], eeo.get(EEO_KEYS[q.key], ""), src + f"voluntary_disclosures.{EEO_KEYS[q.key]}")
    if q.key == "country" or (COUNTRY_RE.search(label) and not PHONE_CODE_RE.search(label)):
        country = ident.get("country", "United States")
        if q.kind in ("text", "textarea") or not q.options:
            return Answer(q.key, country, src + "identity.country")
        for alias in COUNTRY_ALIASES.get(country.lower(), [country]):
            hit = next((o for o in q.options if o.strip().lower() == alias.lower()), None)
            if hit:
                return Answer(q.key, hit, src + "identity.country")
        return _choose(q, country, src + "identity.country")
    if FULL_NAME_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, f"{ident.get('first_name', '')} {ident.get('last_name', '')}".strip(),
                      src + "identity.first_name + last_name")
    if q.kind == "location":
        return Answer(q.key, ident.get("form_city") or ident.get("city", ""), src + "identity.form_city")
    # Phone and mailing-address blocks (Workday and similar forms).
    address = answers.get("address", {})
    if PHONE_TYPE_RE.search(label):
        return _choose(q, address.get("phone_type", "Mobile"), src + "address.phone_type")
    if PHONE_CODE_RE.search(label) and not re.search(r"number", label, re.I):
        if q.kind in ("text", "textarea"):
            return Answer(q.key, "+1", src + "identity.phone (country code)")
        picks = [o for o in q.options if re.search(r"united states", o, re.I) and "+1" in o] or \
                [o for o in q.options if re.search(r"united states", o, re.I)]
        return Answer(q.key, picks[0], src + "identity.phone (country code)") if picks else \
            (Answer(q.key, "United States of America (+1)", src + "identity.phone (country code)") if not q.options
             else Blocker(q.key, label, "no United States (+1) option"))
    if ADDRESS_LINE_RE.search(label) and q.kind in ("text", "textarea"):
        line = address.get("line2" if re.search(r"line ?2", label, re.I) else "line1", "")
        if line:
            return Answer(q.key, line, src + "address.line1/line2")
        return Answer(q.key, "", "left blank (no address in answers.toml)") if not q.required else \
            Blocker(q.key, label, "asks for your street address (set address.line1 in answers.toml)")
    if PLAIN_CITY_RE.search(label) and q.kind in ("text", "textarea"):
        city = address.get("city") or ident.get("form_city", "")
        return Answer(q.key, city, src + ("address.city" if address.get("city") else "identity.form_city"))
    if PLAIN_STATE_RE.search(label):
        return _choose(q, person.state_name(ident.get("state", "")), src + "identity.state")
    if POSTAL_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, str(ident.get("postal_code", "")), src + "identity.postal_code")

    for rx, field in NAME_RULES:
        if rx.search(label) and q.kind in ("text", "textarea"):
            return Answer(q.key, str(ident.get(field, "")), src + f"identity.{field}")
    for rx, field in LINK_RULES:
        if rx.search(label) and q.kind in ("text", "textarea"):
            if links.get(field):
                return Answer(q.key, links[field], src + f"links.{field}")
            if field in ("reel", "artstation", "behance") and links.get("portfolio"):
                return Answer(q.key, links["portfolio"], src + "links.portfolio")
            break  # no link of that kind on file: the drafter or you

    # History with this company. Checked before sponsorship, since "Visa" is also a company.
    if RELATIVE_RE.search(label):
        kin = misc.get("relatives_at_companies", "")
        if kin == "":
            return None
        has = any(c.lower() in company.lower() or company.lower() in c.lower() for c in kin if c)
        return _yes_no(q, has, src + "misc.relatives_at_companies")
    if WORKED_HERE_RE.search(label) or (PREVIOUS_EMPLOYEE_RE.search(label) and not REFERRED_RE.search(label)):
        return _yes_no(q, bool(misc.get("previously_worked_here", False)), src + "misc.previously_worked_here")
    if INTERVIEWED_RE.search(label):
        before = any(c.lower() == company.lower() for c in misc.get("previously_interviewed_at", []))
        return _yes_no(q, before, src + "misc.previously_interviewed_at")
    if APPLIED_BEFORE_RE.search(label):
        return Blocker(q.key, label, "asks whether you've applied here before; only you know")

    # Work-permit end date and visas you hold or held, before the sponsorship rules ("H-1B"
    # appears in both kinds of question).
    if WORK_PERMIT_END_RE.search(label) and not re.search(r"sponsor", label, re.I):
        end = auth.get("opt_end_date", "")
        if not end:
            return None
        y, m, d = (end.split("-") + ["", "", ""])[:3]
        return Answer(q.key, f"{m}/{d}/{y}", src + "work_authorization.opt_end_date")
    if STEM_OPT_RE.search(label) and re.search(r"eligib", label, re.I) and not re.search(r"sponsor", label, re.I):
        return _yes_no(q, bool(auth.get("stem_opt_eligible")), src + "work_authorization.stem_opt_eligible")
    if VISA_NAME_RE.search(label) and VISA_HELD_RE.search(label) and not re.search(r"sponsor", label, re.I):
        have = " ".join([auth.get("current_status", "")] + list(auth.get("previous_visas", []))).lower()
        names = {re.sub(r"[^a-z0-9]", "", n.lower()) for n in VISA_NAME_RE.findall(label)}
        held = {re.sub(r"[^a-z0-9]", "", n) for n in VISA_NAME_RE.findall(have)}
        if "f1" in held:
            held.add("opt")  # OPT is F-1 status
        if not have.strip():
            return None
        return _yes_no(q, bool(names & held), src + "work_authorization.current_status + previous_visas")

    # Work authorization and sponsorship: fixed answers, or a Blocker when phrased oddly.
    # "Will you require sponsorship (for work authorization)?" is a sponsorship question.
    if SPONSOR_NEED_RE.search(label) and not RELOCATE_RE.search(label):
        if NEGATION_RE.search(label):
            return Blocker(q.key, label, "sponsorship question phrased with a negation; answer yourself")
        return _yes_no(q, bool(auth.get("requires_sponsorship")), src + "work_authorization.requires_sponsorship")
    if STATUS_RE.search(label):
        status = auth.get("current_status", "")
        if q.kind in ("text", "textarea"):
            return Answer(q.key, status, src + "work_authorization.current_status")
        hit = next((o for o in q.options if re.search(r"f-?1|\bopt\b", o, re.I)), None)
        return Answer(q.key, hit, src + "work_authorization.current_status") if hit else \
            Blocker(q.key, label, f"no option matches {status}")
    if AUTH_RE.search(label):
        # Only an actual sponsorship clause makes this a combined question ("immigration
        # timelines" is just context).
        if NEGATION_RE.search(label) or re.search(r"sponsor", label, re.I):
            return Blocker(q.key, label, "work-authorization question combined with sponsorship or a negation; answer yourself")
        return _yes_no(q, bool(auth.get("authorized_to_work_in_us")), src + "work_authorization.authorized_to_work_in_us")
    if SPONSOR_RE.search(label) and not RELOCATE_RE.search(label):
        if NEGATION_RE.search(label):
            return Blocker(q.key, label, "sponsorship question phrased with a negation; answer yourself")
        return _yes_no(q, bool(auth.get("requires_sponsorship")), src + "work_authorization.requires_sponsorship")
    if CITIZEN_RE.search(label):
        if re.search(r"authori|eligible", label, re.I):
            return Blocker(q.key, label, "mixes citizenship and work authorization; answer yourself")
        return _yes_no(q, bool(auth.get("us_citizen")), src + "work_authorization.us_citizen")
    if CLEARANCE_RE.search(label):
        if re.search(r"obtain|eligible|able to|willing", label, re.I):
            return Blocker(q.key, label, "asks whether you could obtain a clearance")
        return _yes_no(q, bool(auth.get("security_clearance")), src + "work_authorization.security_clearance")
    if AGE_RE.search(label):
        return _yes_no(q, bool(misc.get("over_18", True)), src + "misc.over_18")

    # Standard yes/no questions answered from [misc] in answers.toml ("" = ask you).
    for rx, field in ((ACCOMMODATION_RE, "needs_accommodation"), (ESSENTIAL_RE, "can_perform_essential_functions"),
                      (GOVERNMENT_RE, "government_employee")):
        if rx.search(label) and (q.kind in ("text", "textarea") or q.options):
            value = misc.get(field, "")
            return None if value == "" else _yes_no(q, bool(value), src + f"misc.{field}")

    # Demographics by label (forms that don't use Greenhouse's standard ids).
    for rx, field in ((GENDER_RE, "gender"), (HISPANIC_RE, "hispanic_latino"), (RACE_RE, "race_ethnicity"),
                      (VETERAN_RE, "veteran_status"), (DISABILITY_RE, "disability_status")):
        if rx.search(label):
            return _eeo(q, field, eeo.get(field, ""), src + f"voluntary_disclosures.{field}")
    if ORIENTATION_RE.search(label):
        return _decline_or(q, "Decline to self-identify", src + "(always declined)")
    if PRONOUN_RE.search(label):
        return _decline_or(q, eeo.get("pronouns", ""), src + "voluntary_disclosures.pronouns")

    # Referrals and sourcing.
    if REFERRED_RE.search(label):
        if q.kind in ("text", "textarea"):
            return Answer(q.key, "", "left blank (no referral)") if not q.required else \
                Blocker(q.key, label, "asks for a referrer's name")
        return _yes_no(q, False, "no referral")
    if HEARD_RE.search(label):
        heard = misc.get("how_did_you_hear", "Company careers page")
        if q.kind in ("text", "textarea"):
            return Answer(q.key, heard, src + "misc.how_did_you_hear")
        for wanted in (heard, "company website", "careers page", "career site", "website", "job board", "online", "other"):
            hit = match_option(q.options, wanted)
            if hit:
                return Answer(q.key, hit, src + "misc.how_did_you_hear")
        return Blocker(q.key, label, f"no 'how did you hear' option fits: {q.options[:8]}")

    if LANGUAGE_RE.search(label):
        spoken = misc.get("spoken_languages", [])
        if q.kind in ("text", "textarea"):
            return Answer(q.key, ", ".join(spoken), src + "misc.spoken_languages") if spoken else None
        picks = [hit for hit in (match_option(q.options, lang) for lang in spoken) if hit]
        if picks:
            return Answer(q.key, " | ".join(picks) if q.kind == "multiselect" else picks[0], src + "misc.spoken_languages")
        return Blocker(q.key, label, "no option matches misc.spoken_languages")

    # Current job, location, preferences, salary.
    if EMPLOYER_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, job.get("company", ""), src + "current_job.company")
    if JOB_TITLE_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, job.get("title", ""), src + "current_job.title")
    if RELOCATE_RE.search(label):
        # Plain Yes/No only. Option lists ("I'm willing to relocate to this job's location",
        # a list of offices) are a preference the drafter picks from the posting's location.
        if q.kind in ("text", "textarea") or sum(bool(re.match(r"\s*(yes|no)\b", o, re.I)) for o in q.options) == 2:
            return _yes_no(q, bool(prefs.get("willing_to_relocate", True)), src + "preferences.willing_to_relocate")
        return None
    if REMOTE_INTENT_RE.search(label) and (q.kind in ("text", "textarea") or
                                           any(re.match(r"\s*(yes|no)\b", o, re.I) for o in q.options)):
        return _yes_no(q, bool(prefs.get("prefers_remote", False)), src + "preferences.prefers_remote")
    yes_no_options = q.kind in ("text", "textarea") or sum(bool(re.match(r"\s*(yes|no)\b", o, re.I)) for o in q.options) == 2
    if ONSITE_RE.search(label) and not LIVE_NOW_RE.search(label) and yes_no_options and not REMOTE_INTENT_RE.search(label):
        onsite = prefs.get("open_to_onsite", "")
        if onsite != "":
            return _yes_no(q, bool(onsite), src + "preferences.open_to_onsite")
    if re.search(r"preferred name", label, re.I) and q.options and yes_no_options:
        different = ident.get("preferred_name", "") not in ("", ident.get("first_name", ""))
        return _yes_no(q, different, src + "identity.preferred_name")
    resident = RESIDENT_RE.search(label)
    if resident and (q.kind in ("text", "textarea") or q.options):
        home = person.state_name(ident.get("state", "")).lower()
        return _yes_no(q, resident.group(2).strip().lower() == home, src + "identity.state")
    if STATE_RE.search(label):
        return _choose(q, person.state_name(ident.get("state", "")), src + "identity.state")
    if LOCATION_TEXT_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, ident.get("location", ""), src + "identity.location")
    if START_RE.search(label) and q.kind in ("text", "textarea"):
        return Answer(q.key, prefs.get("earliest_start", ""), src + "preferences.earliest_start")
    if START_RE.search(label) and q.kind == "date":
        # Date pickers need a date: two weeks from today ("two weeks after an offer").
        start = date.fromordinal(date.today().toordinal() + 14)
        return Answer(q.key, start.strftime("%m/%d/%Y"), src + "preferences.earliest_start (+14 days)")
    if SALARY_RE.search(label):
        target = int(prefs.get("salary_target", 150000))
        if q.kind in ("text", "textarea"):
            return Answer(q.key, f"${target:,}", src + "preferences.salary_target")
        return _salary_option(q, target)

    # "Do you have N years of experience in <field>?" is a fit question for the drafter.
    if re.search(r"years? of (professional |relevant |hands-on )?experience", label, re.I):
        return None

    # Education.
    if q.section == "education" or SCHOOL_RE.search(label) or DEGREE_RE.search(label) \
            or MAJOR_RE.search(label) or GRAD_RE.search(label) or GPA_RE.search(label):
        if SCHOOL_RE.search(label) or q.key.startswith("school"):
            return _choose(q, edu.get("school", ""), src + "education.school")
        if DEGREE_RE.search(label) or q.key.startswith("degree"):
            for wanted in degree_names(edu.get("degree", "")):
                hit = match_option(q.options, wanted) if q.options else wanted
                if hit:
                    return Answer(q.key, hit, src + "education.degree")
            return Blocker(q.key, label, f"no option matches your degree ({edu.get('degree', '')}) in {q.options[:8]}")
        if MAJOR_RE.search(label) or q.key.startswith("discipline"):
            if q.kind in ("text", "textarea"):
                return Answer(q.key, edu.get("major", ""), src + "education.major")
            for wanted in [edu.get("major", "")] + list(edu.get("related_majors", [])) + [edu.get("undergrad_major", "")]:
                hit = match_option(q.options, wanted) if wanted else None
                if hit:
                    return Answer(q.key, hit, src + "education.major")
            return Blocker(q.key, label, "no matching field of study")
        if GPA_RE.search(label):
            return Answer(q.key, str(edu.get("gpa", "")), src + "education.gpa")
        if GRAD_RE.search(label) and q.kind in ("text", "textarea"):
            return Answer(q.key, person.month_year(str(edu.get("graduation", ""))), src + "education.graduation")
    return None
