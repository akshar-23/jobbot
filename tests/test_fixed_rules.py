"""Expected outcomes for every application-question wording seen in testing.

Run after changing jobbot/apply/fixed.py:  python3 tests/test_fixed_rules.py
Uses tests/fixtures/profile (a test candidate), not your profile.
Prints failures and a pass count; exits 1 on any failure.
"""
import os
from pathlib import Path as _P
# Tests run against a fixed test profile, never your own (and never expose your details).
os.environ["JOBBOT_PROFILE"] = str(_P(__file__).resolve().parent / "fixtures" / "profile")
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from jobbot.apply import fixed, engine
from jobbot.apply.model import Question, Answer, Blocker
from jobbot.settings import load_answers
A, C = load_answers(), engine.load_bot_config()
YN = ["Yes", "No"]
CASES = [  # (label, kind, options, required, company, expected: value string, "BLOCK", or None for Claude)
 ("Are you currently authorized to work in the US?", "select", YN, True, "Stripe", "Yes"),
 ("Will you require Stripe to sponsor you for a US work permit now or in the future?", "select", YN, True, "Stripe", "Yes"),
 ("Are you legally authorized to work in the US without sponsorship?", "select", YN, True, "X", "BLOCK"),
 ("Will you now or in the future require sponsorship for work authorization?", "select", YN, True, "Roblox", "Yes"),
 ("For Snowflake to anticipate possible immigration timelines and obligations, could you confirm you are currently authorized to work in the country to which you are applying?", "boolean", YN, True, "Snowflake", "Yes"),
 ("A “U.S. person” is a citizen, legal permanent resident, or legal temporary resident (i.e., a refugee or asylee) of the United States. Which of the following best describes your “U.S. person” status?", "select",
  ["I am a U.S. person", "I am a citizen of Cuba, Iran, North Korea, or Syria AND I am NOT a U.S. person", "None of the above; I am a citizen of a different country"], True, "Snowflake", "None of the above; I am a citizen of a different country"),
 ("Snowflake will process your personal information in accordance with the Snowflake Candidate Privacy Notice.", "multiselect", ["I have read and agree to the Snowflake Candidate Privacy Notice."], True, "Snowflake", "I have read and agree to the Snowflake Candidate Privacy Notice."),
 ("Please review and acknowledge Roblox's Job Applicant Privacy Notice", "select", ["I acknowledge"], True, "Roblox", "I acknowledge"),
 ("Please review and acknowledge Roblox's Candidate Arbitration Agreement", "select", ["I acknowledge"], True, "Roblox", "BLOCK"),
 ("Applicant Arbitration Agreement Acknowledgement", "multiselect", ["I acknowledge that I have opened, read, and understood the Arbitration Agreement."], True, "OpenAI", "BLOCK"),
 ("I hereby certify that I have not knowingly withheld any information", "multiselect", ["I confirm I have read the above."], True, "OpenAI", "BLOCK"),
 ("As part of our interview process, we may use AI notetakers to transcribe conversations. Please see our candidate privacy policy.", "select", ["Yes, I consent", "No, I do not consent"], True, "Palantir", "BLOCK"),
 ("AI Policy for Application", "select", ["Yes"], True, "Anthropic", "BLOCK"),
 ("Please select the country where you currently reside.", "select", ["Canada", "UK", "US", "Other"], True, "Stripe", "US"),
 ("For the sole purpose of determining export control requirements, what is your country of citizenship?", "select", ["Canada", "India", "Other"], True, "Twitch", "India"),
 ("Have you ever interviewed at Anthropic before?", "select", YN, True, "Anthropic", "No"),
 ("Have you ever worked for Visa?", "select", YN, True, "Visa", "No"),
 ("Have you previously applied to Amazon or its subsidiaries?", "select", YN, True, "Twitch", "BLOCK"),
 ("If this role offers the option to work from a remote location, do you plan to work remotely?", "select", YN, True, "Stripe", "No"),
 ("Do you opt-in to receive WhatsApp messages from Stripe Recruiting?", "select", YN, True, "Stripe", "No"),
 ("Please share my resume and contact information with external Palantir partners for job seeking purposes.", "select", YN, True, "Palantir", "No"),
 ("Are you a resident of California?", "select", YN, True, "Palantir", "Yes"),
 ("Legal Name", "text", [], True, "Roblox", "Test Candidate"),
 ("How did you first hear about this role?", "select", ["LinkedIn", "Company Website", "Referral"], True, "Roblox", "Company Website"),
 ("Please tell us how you heard about this opportunity.", "dropdown", ["BuiltIn", "Palantir Website", "Other"], True, "Palantir", "Palantir Website"),
 ("Language Skill(s) (Check all that apply)", "multiselect", ["English (ENG)", "Spanish (SPA)"], True, "Palantir", "English (ENG)"),
 ("Do you currently hold an active US security clearance?", "select", YN, True, "Palantir", "No"),
 ("Are you eligible to obtain the security clearance specified in the job description?", "select", YN, True, "Palantir", "BLOCK"),
 ("Name Pronunciation | How do you pronounce your name?", "text", [], False, "Palantir", None),
 ("Do you possess 2 years of experience in Go?", "select", YN, True, "Stripe", None),
 ("Why Anthropic?", "textarea", [], True, "Anthropic", None),
 ("Do you have an AWS certification?", "select", YN, False, "X", None),
 ("Veteran Status", "select", ["I am not a protected veteran", "I identify as a protected veteran", "I don't wish to answer"], False, "X", "I am not a protected veteran"),
 ("Disability status", "dropdown", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"], False, "X", "No, I do not have a disability"),
 # Workday-style "My Information" page
 ("Phone Device Type", "select", ["Home", "Mobile", "Work"], True, "NVIDIA", "Mobile"),
 ("Country Phone Code", "select", ["India (+91)", "United States of America (+1)"], True, "NVIDIA", "United States of America (+1)"),
 ("Country", "select", ["India", "United States of America"], True, "NVIDIA", "United States of America"),
 # Address answers follow answers.toml: blank line1 -> Needs you; city = address.city or form_city.
 ("Address Line 1", "text", [], True, "NVIDIA", A.get("address", {}).get("line1") or "BLOCK"),
 ("Address Line 2", "text", [], False, "NVIDIA", ""),
 ("City", "text", [], True, "NVIDIA", A.get("address", {}).get("city") or A["identity"]["form_city"]),
 ("State", "select", ["Arizona", "California", "Colorado"], True, "NVIDIA", "California"),
 ("Postal Code", "text", [], True, "NVIDIA", "94105"),
 ("Phone Number", "text", [], True, "NVIDIA", "+1 555-010-0199"),
 # Added 2026-11-15 after a Workday run (Unity): visas held are status, not sponsorship.
 ("Do you currently hold or have you held an H-1B visa?", "select", YN, True, "Unity", "No"),
 ("Are you currently on an H-1B visa that would require a transfer?", "select", YN, True, "Unity", "No"),
 ("Are you currently on OPT or STEM OPT?", "select", YN, True, "Unity", "Yes"),
 ("Will you now or in the future require sponsorship for employment visa status (e.g. H-1B visa status)?", "select", YN, True, "Unity", "Yes"),
 ("Will you require an H-1B visa?", "select", YN, True, "Unity", "Yes"),
 ("If yes, when does your OPT expire?", "text", [], True, "Unity", "06/23/2027"),
 ("Please select if you were a previous Unity employee", "select", YN, True, "Unity Technologies", "No"),
 ("Were you referred by a current or former employee?", "select", YN, True, "Unity", "No"),
 ("Are you willing to work in the office 3 days a week?", "select", YN, True, "Unity", "Yes"),
 ("Do you currently live within commuting distance of our Seattle office?", "select", YN, True, "Unity", None),
 ("Do you have a preferred name?", "select", YN, True, "Unity", "No"),
 ("Would you be open to other roles at Unity?", "select", YN, False, "Unity", "Yes"),
 # Demographics as meanings (answers.toml 2026-11-15): each form's own wording is found.
 ("Please select your ethnicity", "select", ["Asian (United States of America)", "Hispanic or Latino (United States of America)", "White (United States of America)", "I do not wish to answer"], True, "X", "Asian (United States of America)"),
 ("Race/Ethnicity", "select", ["East Asian", "South Asian", "Southeast Asian", "White", "Prefer not to say"], True, "X", "South Asian"),
 ("Race", "select", ["American Indian or Alaska Native", "Asian Indian", "Chinese", "White"], True, "X", "Asian Indian"),
 ("Race", "select", ["American Indian or Alaska Native", "Black", "White", "Other"], True, "X", "BLOCK"),
 ("Hispanic or Latino", "select", ["Hispanic or Latino", "Not Hispanic or Latino", "I don't wish to answer"], True, "X", "Not Hispanic or Latino"),
 ("Veteran status", "select", ["I am not a veteran", "I identify as one or more of the classifications of protected veterans", "I am a veteran, but I am not a protected veteran", "I do not wish to self-identify"], True, "X", "I am not a veteran"),
 ("Gender identity", "select", ["Man", "Woman", "Non-binary", "Prefer not to say"], True, "X", "Man"),
 ("Sexual orientation", "select", ["Heterosexual", "Gay", "Prefer not to say"], False, "X", "Prefer not to say"),
]
fails = 0
for label, kind, opts, req, co, want in CASES:
    r = fixed.resolve(Question("q", label, kind, req, opts), A, co, C)
    got = "BLOCK" if isinstance(r, Blocker) else (r.value if isinstance(r, Answer) else None)
    if got != want:
        fails += 1; print(f"FAIL {label[:70]!r}: got {got!r} want {want!r} ({getattr(r, 'reason', '')})")
print(f"{len(CASES) - fails}/{len(CASES)} pass")

sys.exit(1 if fails else 0)
