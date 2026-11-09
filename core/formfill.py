"""Fills in job application forms in a real Chrome window.

It never guesses. Anything it can't answer from the CV or the user's facts stops the
application and hands it to the user. So do CAPTCHAs and logins, which it never tries
to get past. Nothing here touches the database, so it can be tested on its own.
"""
import glob
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

SUBMITTED, NEEDS_YOU, NO_FORM = "submitted", "needs_you", "no_form"
# Submit was pressed, so the application may already be with the employer.
CAPTCHA_AFTER_SUBMIT = "The site showed a CAPTCHA after Submit. Solve it in the Chrome window, then press I submitted it."
CODE_AFTER_SUBMIT = "The site emailed you a code. Enter it in the Chrome window, then press I submitted it."
UNCONFIRMED_SUBMIT = (
    "Pressed Submit, but the site didn't confirm it. Check the Chrome window before trying again, so you don't apply twice."
)
MAYBE_SUBMITTED = (CAPTCHA_AFTER_SUBMIT, CODE_AFTER_SUBMIT, UNCONFIRMED_SUBMIT)
GOTO_MS = 45_000
ACTION_MS = 5_000
RESULT_SECONDS = 25
LATE_QUESTION_MS = 2000  # some questions only appear a moment after the one before is answered
CV_READ_MS = 1000  # sites start reading an uploaded CV straight away
PLACE_CHECKS = 8  # half a second apart
MAX_APPLY_CLICKS = 3
MAX_ROUNDS = 3  # answering a question can reveal more questions
MAX_OPTIONS = 300
MAX_TEXT_ANSWER = 300
MAX_LONG_ANSWER = 3000
TEMP_PREFIX = "jobfinder-"

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))

# Never filled in, whatever Gemini says.
_SENSITIVE = re.compile(
    r"password|pass ?word|passcode|passphrase|passwd|\bpwd\b|\bpin\b|memorable word|maiden"
    r"|social security|\bssn\b|national.?insurance|\bn\.?i\.? ?(number|no)\b|\bni#|\bnino\b|passport"
    r"|\bsecret\b|\bbank\b|sort.?code|\biban\b|\bswift\b|\bbic\b|routing number|account (number|no)\b"
    r"|(credit|debit|payment) card|card (number|no|expiry)\b|\bcvv\b|\bcvc\b|security number|\bcc (number|csc|exp|name)"
    r"|date of birth|\bdob\b|d\.o\.b|birth ?(date|day)|year of birth|born on|\bbday\b",
    re.I,
)
_SENSITIVE_SQUEEZED = re.compile(r"dateofbirth|passport|iban|nationalinsurance|sortcode|password|bankdetails", re.I)
# Left blank, or "prefer not to say" if the form insists. Never sent to Gemini.
_DEMOGRAPHIC = re.compile(
    r"gender|\bsex\b|\bmale\b|\bfemale\b|ethnic|\brace\b|\bbame\b|\bcaste\b|religio|belief|\bfaith\b"
    r"|sexual orientation|sexuality|lgbt|transgender|\btrans\b|pronoun|veteran|armed forces|disab|health condition"
    r"|neuro-?(diver|typical)|\bage\b|age (range|group|band)|how old|marital|married|children|caring responsib"
    r"|social mobility|socio-?economic|hispanic|latino|mother tongue|\bmr\b",
    re.I,
)
_DECLINE = re.compile(r"prefer not|decline|don'?t wish|do not wish|rather not|not to say|not to disclose|choose not", re.I)
# Only ever answered from what the user has said, never inferred from the CV.
_FACTS_ONLY = re.compile(
    r"right to work|authori[sz]\w* to work|legally|eligib\w* to work|work (permit|authori[sz]ation|visa)|work-permit"
    r"|permitted to work|allowed to work|entitled to work|permission to work|work here|without restriction"
    r"|\bvisa\b|sponsor|immigration|citizen|nationalit|\bu\.?s\.? person\b|criminal|convict|unspent|background check|clearance"
    r"|post-employment|non-?compete|restrictive covenant|employment agreement"
    r"|notice|salary|compensation|remuneration|\bpackage\b|day rate|\bwage\b|\bpay\b|\bote\b|on.target|earnings"
    r"|start date|\bstart\b|how soon|earliest|\bbegin\b|availab|when can you"
    r"|live in|living in|do you live|reside|residen|based in|uk-?based|located in|location|relocat|where are you|hometown"
    r"|\bcountry\b|\bcounty\b|\bcity\b|\baddress\b|post ?code|\bzip\b|time ?zone|\bhours\b"
    r"|willing|able to (commit|work|attend|travel)|commit to|comfortable (with )?(working|being)|commut|on-?site"
    r"|in[- ]person|hybrid|\boffices?\b|travel",
    re.I,
)
# Something the user must personally agree to. Done only with their standing permission.
_CONSENT = re.compile(
    r"\bconsent|\bagree\b|\backnowledg|\bprivacy\b|\bterms\b|t ?& ?c|\bgdpr\b|data (protection|processing|retention)"
    r"|i (have )?read|i've read|i understand|\bcertify\b|i confirm|i declare|i accept|true and (accurate|correct)"
    r"|keep my|retain my|store my|share my",
    re.I,
)
_AFFIRM = re.compile(r"^\W*(yes|i agree|agree|i accept|accept|i consent|consent|i('ve| have) read|i understand|i confirm|confirm)", re.I)
_CAPTCHA_QUESTION = re.compile(
    r"captcha|security question|anti-?robot|not a robot|are you (a )?human|prove you|"
    r"type the (characters|letters|text|words)|what is \d+\s*[-+x*]\s*\d+",
    re.I,
)
_HONEYPOT = re.compile(r"honeypot|leave (this |it )?(blank|empty)|do not (fill|complete)|don'?t fill", re.I)
_NOT_THE_APPLICANT = re.compile(
    r"company|employer|school|university|refer|\bref\d|manager|supervisor|emergency|prefer|recruiter|pronounc|nick"
    r"|partner|spouse|guardian|next of kin|alternate|alternative|secondary|colleague|friend|\bof\b",
    re.I,
)
_OTHER_PERSON = re.compile(r"emergency|referee|reference|next of kin|guardian|supervisor|who should we call", re.I)
_CONTACT = (
    ("first_name", re.compile(r"\b(first|given|fore)[ _-]?name\b|^forename", re.I)),
    ("last_name", re.compile(r"\b(last|family|sur)[ _-]?name\b|^surname", re.I)),
    ("full_name", re.compile(r"^(your |full |legal |candidate )?name\b|\bfull[ _-]?name\b", re.I)),
    ("email", re.compile(r"\be-?mail\b", re.I)),
    ("phone", re.compile(r"\b(phone|mobile|telephone|contact number|tel)\b", re.I)),
    ("linkedin", re.compile(r"linked\s?in", re.I)),
    ("github", re.compile(r"git\s?hub", re.I)),
)
_USERNAME = re.compile(r"user\s?name|handle", re.I)
_RESUME = re.compile(r"resume|r[eé]sum[eé]|\bcv\b|curriculum", re.I)
_SOMEONE_ELSES_FILE = re.compile(r"friend|refer|colleague|partner|\bshare\b|someone|another", re.I)
_COVER = re.compile(r"cover(ing)?[ _-]?letter", re.I)
_MESSAGE_BOX = re.compile(r"^(your )?cover(ing)?[ _-]?letter( \(optional\))?$|^additional (information|info|comments)$", re.I)
_NOT_AN_APPLICATION = re.compile(r"subscribe|search|sign ?up|log ?in|sign ?in|register|newsletter|contact us|filter", re.I)
_UNRELATED_QUESTION = re.compile(r"^(subject|topic)$|friend'?s? |refer a friend|complaint|what went wrong|newsletter", re.I)

_DIAL_CODE = re.compile(r"\+(\d{1,4})$")
# Boxes that suggest places as you type. Some sites only accept a place picked from the list.
_PLACE = re.compile(r"location|\bcity\b|\baddress\b", re.I)
_UK = "uk|gbr|united kingdom|great britain|england|scotland|wales"
_PLACE_NAMES = {"uk": _UK, "united kingdom": _UK, "great britain": _UK, "usa": "usa|us|united states", "united states": "usa|us|united states"}
_SUGGESTIONS = '[role=option], [class*="dropdown-location"], [class*="suggestion"], [class*="autocomplete"] li'

_APPLY_LINK = re.compile(
    r"^\s*(apply|apply now|apply here|apply for (this|the) (job|position|role)|apply to (this )?(job|position|role)"
    r"|start (your )?application|i'?m interested)\b",
    re.I,
)
_APPLICATION_TAB = re.compile(r"^\s*application\s*$", re.I)

_SUCCESS = re.compile(
    r"thank you for (applying|your application|submitting)|thanks for applying"
    r"|application (has been |was )?(received|submitted|sent)(?! (date|back)\b)|we('ve| have) received your application"
    r"|successfully (submitted|applied|sent)",
    re.I,
)
_NOT_SUCCESS = re.compile(r"\bnot\b|n't\b|\bunable\b|\bsorry\b|\bfailed\b|\berror\b|\bonce\b|\bafter\b|\bbefore\b|\bwhen\b|\bif\b|\buntil\b", re.I)
_SUCCESS_URL = re.compile(r"/(confirmation|confirmed|thank-?you|thanks|success|submitted)/?$", re.I)
_BAD_QUERY = re.compile(r"error|fail|invalid", re.I)
_CODE_PROMPT = re.compile(r"security code|verification code|enter the code|code (we|that was) sent", re.I)
_BACKGROUND_REQUEST = re.compile(r"collect|analytics|pageview|log_event|doubleclick|sentry|telemetry|hcaptcha|recaptcha", re.I)

_FIND_FIELDS = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    if (!el || !el.isConnected) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  // Fields a person can't see are traps for bots. Filling one gets an application thrown away.
  const trap = el => {
    if (el.tabIndex === -1 || el.closest('[aria-hidden="true"]')) return true;
    const r = el.getBoundingClientRect();
    if (r.right + scrollX < 0 || r.bottom + scrollY < 0 || r.width < 4 || r.height < 4) return true;
    for (let n = el, i = 0; n && i < 5; n = n.parentElement, i++) {
      const s = getComputedStyle(n);
      if (s.opacity === '0' || /rect\(0/.test(s.clip)) return true;
    }
    return false;
  };
  const words = node => {  // what a person sees: no hidden helper text, and no options from a select inside a label
    const parts = [];
    const walk = parent => {
      for (const child of parent.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) parts.push(child.textContent);
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        if (child.matches('select, input, textarea, button, script, style, svg, [role=listbox]')) continue;
        const s = getComputedStyle(child);
        if (s.display !== 'none' && s.visibility !== 'hidden') walk(child);
      }
    };
    walk(node);
    return clean(parts.join(' '));
  };
  const GENERIC = /^(attach|upload( file)?|browse|choose file|drop.*here)$/i;
  const HEADING = 'label, legend, [class*="label" i], [class*="question" i], [class*="title" i], h2, h3, h4, h5, h6, p';
  const headingBefore = el => {
    let node = el;
    for (let depth = 0; depth < 6 && node.parentElement && node.parentElement !== document.body; depth++) {
      node = node.parentElement;
      const before = [...node.querySelectorAll(HEADING)].filter(c => !c.contains(el)
        && (c.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) && words(c) && !GENERIC.test(words(c)));
      if (before.length) return before[before.length - 1];
    }
    return null;
  };
  const describe = el => {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) {
      const nodes = ids.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
      const text = clean(nodes.map(words).join(' '));
      if (text) return {text, node: nodes[0]};
    }
    if (el.labels && el.labels.length) {
      const text = clean([...el.labels].map(words).join(' '));
      if (text && !GENERIC.test(text)) return {text, node: el.labels[0]};
    }
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return {text: aria, node: el};
    const heading = headingBefore(el);
    if (heading) return {text: words(heading), node: heading};
    return {text: clean(el.placeholder || el.name || el.id), node: el};
  };
  const hints = el => {
    const described = (el.getAttribute('aria-describedby') || '').split(/\s+/).map(id => document.getElementById(id))
      .filter(Boolean).map(n => n.textContent).join(' ');
    return clean([el.placeholder, el.title, el.getAttribute('autocomplete'), described].filter(Boolean).join(' '));
  };
  const sectionOf = el => {
    const box = el.closest('fieldset');
    const legend = box && box.querySelector('legend');
    if (legend) return clean(legend.textContent);
    let node = el;
    for (let depth = 0; depth < 4 && node.parentElement; depth++) {
      node = node.parentElement;
      const before = [...node.querySelectorAll('h1, h2, h3, h4')].filter(h => !h.contains(el)
        && (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING));
      if (before.length) return clean(before[before.length - 1].textContent);
    }
    return '';
  };
  const required = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true' || /^(true|required)$/i.test(el.getAttribute('data-required') || '')) return true;
    if (/[*✱]|\(required\)|\brequired\b|\bmandatory\b/i.test(label.text)) return true;
    const near = [el, label.node, el.parentElement, el.parentElement && el.parentElement.parentElement];
    if (near.some(n => n && /required/i.test(String(n.className || '')))) return true;
    const star = `${getComputedStyle(label.node, '::after').content} ${getComputedStyle(label.node, '::before').content}`;  // stars drawn with CSS
    if (/\*|✱|required/i.test(star)) return true;
    return !!el.parentElement && (el.parentElement.innerText || '').length < 200 && /required\b/i.test(el.parentElement.innerText || '');
  };
  const count = root => root.querySelectorAll('input:not([type=hidden]), select, textarea, [role=combobox]').length;
  const forms = [...document.forms].filter(f => shown(f) && count(f) >= 2).sort((a, b) => count(b) - count(a));
  const root = forms[0] || document.body;

  let next = Number(document.documentElement.getAttribute('data-jf-count') || 0);
  const keyOf = el => {
    if (!el.getAttribute('data-jf')) el.setAttribute('data-jf', 'f' + (++next));
    return el.getAttribute('data-jf');
  };
  const base = el => ({name: el.name || '', id: el.id || '', hints: hints(el), section: sectionOf(el), value: '', options: [], maxlength: 0, accept: '', multiple: false});
  const controls = new Set(root.querySelectorAll('input, select, textarea, [role=combobox]'));
  if (root.elements) for (const e of root.elements) if (e.matches('input, select, textarea')) controls.add(e);
  const fields = [];
  const groups = new Map();
  for (const el of controls) {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'input' ? (el.getAttribute('type') || 'text') : tag).toLowerCase();
    const role = el.getAttribute('role');
    if (['hidden', 'submit', 'image', 'reset'].includes(type) || (type === 'button' && tag === 'input') || el.disabled) continue;
    if (/captcha/i.test(el.name || '') && !shown(el)) continue;
    if (type === 'radio' || type === 'checkbox') {
      if (!shown(el) && !(el.labels && shown(el.labels[0]))) continue;
      const box = el.closest('fieldset, [role=group], [role=radiogroup]');
      const pure = box && !box.querySelector('input[type=text], input[type=email], input[type=tel], input[type=url], input[type=file], select, textarea');
      const id = type === 'radio' ? 'r:' + (el.name || keyOf(el)) : (pure ? box : 'c:' + (el.name || keyOf(el)));
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(el);
      continue;
    }
    if (type === 'file') {
      if (el.closest('[class*="autofill" i]')) continue;  // a shortcut that overwrites other fields from the CV
    } else {
      if (!shown(el)) continue;
      if (role !== 'combobox' && trap(el)) continue;
      if (type === 'search' && role !== 'combobox' && !el.required && el.getAttribute('aria-required') !== 'true') continue;
    }
    const label = describe(el);
    const isRequired = required(el, label);
    if (role !== 'combobox' && type !== 'file' && !el.id && !el.name && !(el.labels && el.labels.length)
        && !el.getAttribute('aria-label') && !el.placeholder && !isRequired) continue;
    const pressing = tag === 'button' && el.type === 'submit' && el.form;
    fields.push({
      ...base(el), key: keyOf(el), type, label: label.text, required: isRequired,
      kind: pressing ? 'blocked' : role === 'combobox' ? 'combobox' : tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : type === 'file' ? 'file' : 'text',
      value: el.value || '', maxlength: el.maxLength > 0 ? el.maxLength : 0, accept: el.getAttribute('accept') || '', multiple: !!el.multiple,
      options: tag === 'select' ? [...el.options].filter(o => !o.disabled && o.value !== '' && !/^(select|choose|please select|--)/i.test(clean(o.text))).map(o => clean(o.text)) : [],
    });
  }
  for (const inputs of groups.values()) {
    const first = inputs[0];
    const single = inputs.length === 1 && first.type === 'checkbox';
    const owner = first.closest('[role=radiogroup], [role=group]');
    const heading = headingBefore(first);
    const label = single ? describe(first)
      : owner && (owner.getAttribute('aria-labelledby') || owner.getAttribute('aria-label')) ? describe(owner)
      : heading ? {text: words(heading), node: heading} : describe(first);
    const key = keyOf(first);
    const optionText = i => clean((i.labels && i.labels[0] && words(i.labels[0])) || i.getAttribute('aria-label') || i.value);
    inputs.forEach((i, n) => i.setAttribute('data-jf-option', key + ':' + n));
    fields.push({
      ...base(first), key, type: first.type, label: label.text,
      required: inputs.some(i => required(i, label)) || !!(owner && owner.getAttribute('aria-required') === 'true'),
      kind: single ? 'checkbox' : first.type === 'radio' ? 'radio' : 'checkboxes',
      value: single && first.checked ? 'checked' : '', options: inputs.map(optionText), multiple: first.type === 'checkbox',
    });
  }
  for (const yes of root.querySelectorAll('button')) {
    if (!/^yes$/i.test(clean(yes.innerText)) || !shown(yes)) continue;
    const no = [...yes.parentElement.querySelectorAll('button')].find(b => /^no$/i.test(clean(b.innerText)));
    if (!no) continue;
    const heading = headingBefore(yes.parentElement);
    const label = heading ? {text: words(heading), node: heading} : {text: '', node: yes};
    const key = keyOf(yes);
    yes.setAttribute('data-jf-option', key + ':0');
    no.setAttribute('data-jf-option', key + ':1');
    const hidden = yes.parentElement.parentElement.querySelector('input[type=checkbox]');
    const pressing = yes.type === 'submit' && yes.form;  // pressing this would send the form early
    fields.push({
      ...base(yes), key, type: 'buttons', label: label.text, required: required(hidden || yes, label),
      kind: pressing ? 'blocked' : 'radio', options: ['Yes', 'No'],
    });
  }
  document.documentElement.setAttribute('data-jf-count', String(next));

  // Only a button that plainly means "send my application" may be pressed.
  const SUBMIT = /^(submit|send|apply)( (my|your))?( (job )?(application|form))?( now)?$|^apply for (this|the) (job|position|role)$|^(complete|finish)( (my|your))? application$/i;
  document.querySelectorAll('[data-jf-submit]').forEach(b => b.removeAttribute('data-jf-submit'));
  const buttons = [...root.querySelectorAll('button, input[type=submit], [role=button]')].filter(shown);
  const text = b => clean(b.innerText || b.value || b.getAttribute('aria-label')).replace(/[^a-z ]/gi, '').trim();
  const sending = buttons.filter(b => SUBMIT.test(text(b)));
  const submit = sending.filter(b => /^(submit|send)/i.test(text(b))).pop() || sending.pop();
  if (submit) submit.setAttribute('data-jf-submit', '1');

  // Checks for what the steps above can't see, made just before pressing Submit.
  const customRequired = [];
  for (const e of root.querySelectorAll('[aria-required="true"], [role=radiogroup], [role=listbox], [role=textbox], [role=radio], [role=checkbox], [contenteditable="true"], button[aria-haspopup="listbox"]')) {
    if (e.matches('input, select, textarea, [role=combobox]') || !shown(e) || e.querySelector('input, select, textarea')) continue;
    const label = describe(e);
    if (required(e, label)) customRequired.push(label.text || 'a question it can\'t read');
  }
  const invalid = [...controls].filter(e => e.matches('input, select, textarea') && !['hidden', 'submit', 'button'].includes(e.type)
      && ((e.willValidate && !e.checkValidity())
        || (e.getAttribute('aria-required') === 'true' && e.getAttribute('role') !== 'combobox' && !e.value && !['checkbox', 'radio', 'file'].includes(e.type) && shown(e))))
    .map(e => clean(describe(e).text)).filter(Boolean);
  return {
    fields,
    submitLabel: submit ? text(submit) : '',
    submitTypes: buttons.filter(b => b.type === 'submit').map(text),
    nextStep: !submit && buttons.some(b => /^(next|continue)\b/i.test(text(b))),
    password: [...document.querySelectorAll('input[type=password]')].some(shown),
    customRequired: [...new Set(customRequired)],
    invalid: [...new Set(invalid)],
    shadow: [...root.querySelectorAll('*')].some(e => e.shadowRoot && e.shadowRoot.querySelector('input, select, textarea')),
  };
}
"""

_READ_OPTIONS = r"""
key => {
  document.querySelectorAll('[data-jf-choice]').forEach(o => o.removeAttribute('data-jf-choice'));
  const box = document.querySelector(`[data-jf="${key}"]`);
  const id = box && box.getAttribute('aria-controls');
  const list = id && document.getElementById(id);
  const all = (list || document).querySelectorAll('[role=option]');
  const seen = [];
  for (const o of all) {
    const r = o.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && getComputedStyle(o).visibility !== 'hidden') {
      o.setAttribute('data-jf-choice', String(seen.length));
      seen.push((o.innerText || '').replace(/\s+/g, ' ').trim());
    }
  }
  return seen;
}
"""

_DISMISS_COOKIES = r"""
() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  const inBanner = button => {
    for (let n = button.parentElement, i = 0; n && n !== document.body && i < 7; n = n.parentElement, i++) {
      if (/cookie|consent|onetrust|gdpr/i.test(`${n.id} ${n.className} ${n.getAttribute('aria-label') || ''}`)) return true;
      if ((n.innerText || '').length < 1500 && /\bcookies?\b/i.test(n.innerText || '')) return true;
    }
    return false;
  };
  const buttons = [...document.querySelectorAll('button, [role=button], a')].filter(b => shown(b) && !b.closest('form'));
  const patterns = [/^(reject( all)?( cookies)?|deny|decline( all)?|(only )?necessary( cookies)?( only)?)$/i, /^(accept( all)?( cookies)?|allow( all)?( cookies)?|i accept|got it|ok(ay)?)$/i];
  for (const pattern of patterns) {
    const hit = buttons.find(b => pattern.test(clean(b.innerText)) && inBanner(b));
    if (hit) { hit.click(); return true; }
  }
  return false;
}
"""

_CAPTCHA = r"""
beforeSubmit => {
  const big = beforeSubmit ? 40 : 100;
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width >= big && r.height >= big / 2 && r.top > -1000;
  };
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.src || '', title = f.title || '';
    if (!visible(f) || src.includes('invisible')) continue;
    if (beforeSubmit && /recaptcha\/.*anchor|hcaptcha.*frame=checkbox|challenges\.cloudflare|arkoselabs|funcaptcha|geetest/i.test(src)) return true;
    if (!beforeSubmit && /recaptcha\/.*bframe|hcaptcha.*frame=challenge|challenges\.cloudflare|arkoselabs|funcaptcha|geetest/i.test(src + title)) return true;
    if (!beforeSubmit && /recaptcha challenge|hcaptcha challenge/i.test(title)) return true;
  }
  if (beforeSubmit) {
    const widgets = '.g-recaptcha:not([data-size="invisible"]), .h-captcha:not([data-size="invisible"]), .cf-turnstile, [id*="funcaptcha" i], [class*="geetest" i], [id*="geetest" i], [id*="arkose" i], [class*="arkose" i]';
    for (const w of document.querySelectorAll(widgets)) if (visible(w)) return true;
  }
  return false;
}
"""

_FORM_ERRORS = r"""
() => {
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  const alerts = [...document.querySelectorAll('[role=alert], [aria-live=assertive]')].filter(shown).map(a => a.innerText.trim()).filter(Boolean);
  const invalid = [...document.querySelectorAll('[aria-invalid=true]')].filter(shown).length;
  return {alerts: alerts.slice(0, 3), invalid};
}
"""


class FormError(Exception):
    """Message is shown to the user."""


@dataclass
class Field:
    key: str
    kind: str  # text, textarea, select, combobox, radio, checkbox, checkboxes, file, blocked
    type: str
    label: str
    required: bool
    name: str = ""
    id: str = ""
    hints: str = ""  # placeholder, tooltip and the like: pages can hide what a field is really for here
    section: str = ""
    value: str = ""
    options: list = field(default_factory=list)
    maxlength: int = 0
    accept: str = ""
    multiple: bool = False

    @property
    def clean_label(self):
        return re.sub(r"\s*✱.*$|\s*\*\s*$", "", self.label).strip()

    @property
    def identity(self):
        """Everything that says what this field is for, in a form the rules can match."""
        text = unicodedata.normalize("NFKC", f"{self.clean_label} {self.hints} {self.name} {self.id}").translate(_INVISIBLE)
        split = re.sub(r"[_\-\[\]]+", " ", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text))
        return f"{text} {split}"

    @property
    def sensitive(self):
        # Squeezed, because a label can be split across elements: "Da te of bi rth".
        squeezed = re.sub(r"\s+", "", self.identity)
        return self.type == "password" or bool(_SENSITIVE.search(self.identity) or _SENSITIVE_SQUEEZED.search(squeezed))

    @property
    def facts_only(self):
        # A long open question can mention a location in passing without being about the candidate's.
        if self.kind == "textarea" and len(self.clean_label) > 200:
            return False
        return bool(_FACTS_ONLY.search(self.identity))


@dataclass
class Answer:
    values: list
    source: str  # facts, cv, writing, message, contact, unknown


@dataclass
class Job:
    url: str
    full_name: str
    email: str
    phone: str
    linkedin: str
    message: str
    resume_name: str
    resume_bytes: bytes
    cover_name: str
    cover_bytes: bytes
    allow_consent: bool
    submit: bool  # may press Submit when nothing is in the way
    stay_open: bool  # the user is watching, so leave the window for them
    ask: Callable  # list of Fields -> {key: Answer}, from Gemini
    github: str = ""  # a profile link, https://github.com/you


@dataclass
class Outcome:
    state: str
    note: str = ""
    answers: list = field(default_factory=list)  # what went into the form, for the user to see


def _public_navigation(route):
    """A job page must not be able to send this browser to your router or this dashboard."""
    request = route.request
    if request.is_navigation_request() and not is_public(request.url):
        route.abort()
    else:
        route.continue_()


def is_public(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return parts.scheme in ("about", "blob", "data")
        for result in socket.getaddrinfo(parts.hostname, parts.port or None, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(result[4][0].split("%")[0])
            if not (getattr(ip, "ipv4_mapped", None) or ip).is_global:
                return False
    except (OSError, ValueError):
        return False
    return True


class Chrome:
    """The user's installed Chrome with a throwaway profile, and a separate window per application."""

    def __init__(self, headless=False):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._kept = []  # (context, page, temp folder) of windows left for the user

    def __enter__(self):
        # CV copies left behind if the app was killed mid-form. Nothing else is using them now.
        for stale in glob.glob(os.path.join(tempfile.gettempdir(), f"{TEMP_PREFIX}*")):
            shutil.rmtree(stale, ignore_errors=True)
        self._playwright = sync_playwright().start()
        try:
            self._launch()
        except FormError:
            self._playwright.stop()
            raise
        return self

    def __exit__(self, *exc_info):
        for context, _, temp in self._kept:
            _close(context)
            temp.cleanup()
        self._kept = []
        try:
            self._browser.close()
        except PlaywrightError:
            pass
        self._playwright.stop()

    def _launch(self):
        try:
            self._browser = self._playwright.chromium.launch(channel="chrome", headless=self.headless)
        except PlaywrightError as exc:
            raise FormError("Couldn't open Google Chrome. Install it from google.com/chrome to use form filling.") from exc

    def new_window(self):
        if not self._browser.is_connected():  # the user quit Chrome
            self._launch()
        context = self._browser.new_context(locale="en-GB", accept_downloads=False, no_viewport=not self.headless)
        context.route("**/*", _public_navigation)
        return context

    def keep(self, context, page, temp):
        self._kept.append((context, page, temp))

    def windows_open(self):
        still_open = []
        for context, page, temp in self._kept:
            if page.is_closed() or not self._browser.is_connected():
                _close(context)
                temp.cleanup()
            else:
                still_open.append((context, page, temp))
        self._kept = still_open
        return bool(still_open)

    def wait(self, ms):
        """Lets Chrome's events through, which is how a closed window gets noticed."""
        try:
            self._kept[0][1].wait_for_timeout(ms)
        except (PlaywrightError, IndexError):
            pass


def _close(context):
    try:
        context.close()
    except PlaywrightError:
        pass


def render(url):
    """The page's HTML after its JavaScript has run. For job boards that are empty without it."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(locale="en-GB", accept_downloads=False)
            context.route("**/*", _public_navigation)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS)
            _settle(page)
            return page.content()
        finally:
            browser.close()


def apply(chrome, job):
    """Fills in one application. Returns an Outcome and never raises for a problem on the page."""
    context = chrome.new_window()
    temp = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
    answers = []
    keep = False
    page = None
    try:
        page = context.new_page()
        outcome, keep, page = _run(context, page, job, Path(temp.name), answers)
    except PlaywrightError as exc:
        log.warning("Form filling stopped on %s: %s", job.url, exc)
        message = str(exc).splitlines()[0][:160] if str(exc) else "unknown"
        outcome = Outcome(NEEDS_YOU, f"The browser hit a problem on this form ({message}).")
    except FormError as exc:
        outcome = Outcome(NEEDS_YOU, str(exc))
    except Exception as exc:
        log.exception("Form filling crashed on %s", job.url)
        outcome = Outcome(NEEDS_YOU, f"Something went wrong while filling in this form ({str(exc)[:160]}).")
    outcome.answers = answers
    if keep and page and not page.is_closed():
        chrome.keep(context, page, temp)
    else:
        _close(context)
        temp.cleanup()
    return outcome


def _run(context, page, job, folder, answers):
    """Returns (outcome, leave the window open?, the page the form is on)."""
    page.goto(job.url, wait_until="domcontentloaded", timeout=GOTO_MS)
    _settle(page)
    _dismiss_cookies(page)
    page, frame, found = _find_form(context, page)
    if frame is None:
        if found and found["password"]:
            return Outcome(NEEDS_YOU, "This site wants you to log in or create an account, so you'll need to apply there yourself."), job.stay_open, page
        return Outcome(NO_FORM, "Couldn't find an application form on this page."), job.stay_open, page

    blockers = []
    handled = set()
    _attach_files_first(page, frame, job, folder, answers, blockers, handled)
    for _ in range(MAX_ROUNDS):
        fields = [_field(raw) for raw in frame.evaluate(_FIND_FIELDS)["fields"]]
        fresh = [item for item in fields if item.key not in handled]
        if not fresh:
            break
        handled.update(item.key for item in fresh)
        _fill_round(page, frame, fresh, job, folder, answers, blockers)
        page.wait_for_timeout(400)

    page.wait_for_timeout(LATE_QUESTION_MS)
    state = frame.evaluate(_FIND_FIELDS)
    blockers.extend(_final_checks(state, handled, page, job, answers, blockers))
    if blockers:
        note = "Needs you: " + "; ".join(dict.fromkeys(blockers)) + "."
        if any(blocker.startswith("No answer") for blocker in blockers):
            note += " Add what's missing to this CV's facts in Setup and press Fill the form, or finish it yourself."
        return Outcome(NEEDS_YOU, note), job.stay_open, page
    if not job.submit:
        note = "The form is filled in. Check it in the Chrome window and press Submit there." if job.stay_open else "It wasn't submitted."
        return Outcome(NEEDS_YOU, note), job.stay_open, page

    traffic = _Traffic(page)
    before = _all_text(page)
    start_url = page.url
    frame.locator("[data-jf-submit]").first.click(timeout=ACTION_MS)  # once: a retry could apply twice
    outcome, keep = _await_result(page, before, start_url, traffic)
    return outcome, keep or job.stay_open, page


def _final_checks(state, handled, page, job, answers, blockers):
    """What must be true before pressing Submit, beyond each question having an answer."""
    problems = []
    for raw in state["fields"]:
        if raw["key"] not in handled and raw["required"]:
            problems.append(f'No answer for "{_field(raw).clean_label}"')
    answered = {answer["label"] for answer in answers}
    for label in state["customRequired"]:
        if re.sub(r"\s*✱.*$|\s*\*\s*$", "", label).strip() not in answered:
            problems.append(f'It has a required question it can\'t fill in: "{label[:120]}"')
    if state["shadow"]:
        problems.append("Part of the form can't be read")
    named = " ".join(blockers + problems)
    for label in state["invalid"]:
        clean = re.sub(r"\s*✱.*$|\s*\*\s*$", "", label).strip()
        if clean not in named:
            problems.append(f'The site won\'t accept the answer for "{clean[:120]}" yet')
    if state["password"]:
        problems.append("It asks for a password or account")
    if state["nextStep"]:
        problems.append("It has more steps, which need you to go through them")
    elif not state["submitLabel"]:
        problems.append("Couldn't find a button that plainly submits the application")
    if not any(answer["answer"] == job.resume_name for answer in answers):
        problems.append("Your CV isn't attached, so it's not worth sending by itself")
    if any(_safe_evaluate(frame, _CAPTCHA, True) for frame in page.frames):
        problems.append("It shows a CAPTCHA, which only you can do")
    return problems


def _field(raw):
    return Field(**{name: raw[name] for name in Field.__dataclass_fields__ if name in raw})


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightError:
        pass  # busy pages never go quiet; the content is there by now


def _dismiss_cookies(page):
    """Cookie banners can sit on top of the Submit button. Refuses them where possible."""
    if _safe_evaluate(page.main_frame, _DISMISS_COOKIES):
        page.wait_for_timeout(300)


def _find_form(context, page):
    found = None
    for attempt in range(MAX_APPLY_CLICKS + 1):
        frame, found = _best_frame(page)
        if frame is not None:
            return page, frame, found
        if found and found["password"] or attempt == MAX_APPLY_CLICKS:
            break
        if not _click_apply(page):
            break
        page.wait_for_timeout(1500)
        page = context.pages[-1]  # the link may have opened a new tab
        _settle(page)
        _dismiss_cookies(page)
    return page, None, found


def _click_apply(page):
    """Follows a link to the form. Never a button that belongs to a form, which could send it."""
    control = (
        page.get_by_role("link", name=_APPLY_LINK)
        .or_(page.get_by_role("button", name=_APPLY_LINK))
        .or_(page.get_by_role("tab", name=_APPLICATION_TAB))
        .or_(page.get_by_role("link", name=_APPLICATION_TAB))
    )
    try:
        for index in range(min(control.count(), 5)):
            candidate = control.nth(index)
            if candidate.is_visible() and candidate.evaluate("el => !(el.form && el.type === 'submit')"):
                candidate.click(timeout=ACTION_MS)
                return True
    except PlaywrightError:
        pass
    return False


def _best_frame(page):
    """The frame holding an application form, since employers often embed them in an iframe."""
    best = None
    best_state = None
    any_state = None
    for frame in page.frames:
        state = _safe_evaluate(frame, _FIND_FIELDS)
        if not state:
            continue
        any_state = state if any_state is None or state["password"] else any_state
        if _looks_like_application(state) and (best_state is None or len(state["fields"]) > len(best_state["fields"])):
            best, best_state = frame, state
    return best, best_state or any_state


def _looks_like_application(state):
    """A place for a CV, or enough fields with an email: not a search, newsletter, referral or login box."""
    if state["password"] or any(_NOT_AN_APPLICATION.search(label) for label in state["submitTypes"]):
        return False
    fields = [_field(raw) for raw in state["fields"]]
    if any(_UNRELATED_QUESTION.search(item.clean_label) for item in fields):
        return False
    has_cv_box = any(item.kind == "file" and _is_my_cv_box(item) for item in fields)
    has_email = any(item.type == "email" or re.search(r"e-?mail", item.clean_label, re.I) for item in fields)
    return has_cv_box or (has_email and len(fields) >= 4)


def _is_my_cv_box(item):
    return bool(_RESUME.search(item.identity)) and not _SOMEONE_ELSES_FILE.search(item.clean_label)


def _safe_evaluate(frame, script, argument=None):
    try:
        return frame.evaluate(script, argument) if argument is not None else frame.evaluate(script)
    except PlaywrightError:
        return None


def _attach_files_first(page, frame, job, folder, answers, blockers, handled):
    """Many sites read an uploaded CV, fill in fields from it and rebuild the form. Doing this first
    means an old address from the CV can't overwrite an answer, and the form is read again afterwards."""
    attached = False
    for raw in frame.evaluate(_FIND_FIELDS)["fields"]:
        item = _field(raw)
        if item.kind == "file" and not item.sensitive:
            handled.add(item.key)
            attached = _attach(frame, item, item.clean_label or item.name, job, folder, answers, blockers) or attached
    if attached:
        page.wait_for_timeout(CV_READ_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightError:
            pass


def _fill_round(page, frame, fields, job, folder, answers, blockers):
    questions = []
    for item in fields:
        label = item.clean_label or item.name or "(unlabelled question)"
        if _HONEYPOT.search(item.identity):
            continue
        if item.kind == "file":
            if not item.sensitive:  # one that only appeared later, or was rebuilt after the upload
                _attach(frame, item, label, job, folder, answers, blockers)
        elif item.kind == "blocked":
            blockers.append(f'Its "{label}" question has buttons that could send the form early')
        elif item.sensitive:
            if item.required:
                blockers.append(f'It asks for something sensitive that you should enter yourself: "{label}"')
        elif _CAPTCHA_QUESTION.search(item.identity):
            if item.required:
                blockers.append("It shows a CAPTCHA, which only you can do")
        elif _DEMOGRAPHIC.search(item.identity):
            _answer_demographic(page, frame, item, label, answers, blockers)
        elif item.kind == "checkbox" and not item.facts_only:
            _answer_checkbox(frame, item, label, job, answers, blockers)
        elif not item.facts_only and (_CONSENT.search(item.identity) or any(_CONSENT.search(o) for o in item.options)):
            _answer_consent_question(page, frame, item, label, job, answers, blockers)
        else:
            _answer_by_rule_or_ask(page, frame, item, label, job, answers, blockers, questions)
    if not questions:
        return
    replies = job.ask(questions)
    for item in questions:
        label = item.clean_label or item.name or "(unlabelled question)"
        reply = replies.get(item.key) or Answer([], "unknown")
        values = _usable_values(item, reply)
        if values and _set(page, frame, item, values):
            answers.append({"label": label, "answer": ", ".join(values), "source": reply.source})
        elif item.required:
            blockers.append(f'No answer for "{label}"')


def _answer_by_rule_or_ask(page, frame, item, label, job, answers, blockers, questions):
    contact = _contact_value(item, job)
    if contact is not None:
        if contact and _set(page, frame, item, [contact]):
            answers.append({"label": label, "answer": contact, "source": "contact"})
        elif item.required:
            blockers.append(f'No answer for "{label}"')
        return
    if item.kind in ("text", "textarea") and _MESSAGE_BOX.search(item.clean_label) and job.message:
        if _set(page, frame, item, [job.message]):
            answers.append({"label": label, "answer": job.message, "source": "message"})
        return
    if item.kind == "combobox":
        item.options = _combobox_options(page, frame, item)
        if _is_dial_code_picker(item):  # it belongs to the phone number, not to where you live
            dial_code = _dial_code_option(item.options, job.phone)
            if dial_code and _set(page, frame, item, [dial_code]):
                answers.append({"label": label, "answer": dial_code, "source": "contact"})
            elif item.required:
                blockers.append(f'No answer for "{label}"')
            return
    questions.append(item)


def _is_dial_code_picker(item):
    with_codes = sum(bool(_DIAL_CODE.search(option)) for option in item.options)
    return with_codes > 0 and with_codes * 2 > len(item.options)


def _dial_code_option(options, phone):
    """The option matching the phone number's own country code, longest code first."""
    digits = re.sub(r"[^\d+]", "", phone or "")
    if not digits.startswith("+"):
        return None
    matches = [(option, _DIAL_CODE.search(option)) for option in options]
    matches = [(option, found.group(1)) for option, found in matches if found and digits[1:].startswith(found.group(1))]
    # Several places share +44 and +1, and picking Guernsey would look odd on an application.
    best = max(matches, key=lambda pair: (len(pair[1]), pair[0].startswith(("United Kingdom", "United States"))), default=None)
    return best[0] if best else None


def _contact_value(item, job):
    """The applicant's own details, filled in by rule. None if this isn't a contact field."""
    if item.kind not in ("text", "textarea") or _NOT_THE_APPLICANT.search(item.identity) or _OTHER_PERSON.search(item.section):
        return None
    for name, pattern in _CONTACT:
        if pattern.search(item.clean_label) or (not item.clean_label and pattern.search(item.identity)):
            break
    else:
        if item.type == "email":
            name = "email"
        elif item.type == "tel":
            name = "phone"
        else:
            return None
    if name == "github" and (not job.github or item.kind != "text" or len(item.clean_label) > 60):
        return None  # Gemini answers it from the CV and facts: a question about your work, or no link known
    first, _, last = job.full_name.strip().partition(" ")
    linkedin = job.linkedin
    if linkedin and not linkedin.lower().startswith("http"):
        linkedin = f"https://{linkedin}"
    github = job.github.rstrip("/")
    if github and _USERNAME.search(item.clean_label or item.identity):  # "GitHub username" wants the name, not a link
        github = github.rsplit("/", 1)[-1]
    return {
        "first_name": first, "last_name": last.strip(), "full_name": job.full_name.strip(),
        "email": job.email, "phone": job.phone, "linkedin": linkedin, "github": github,
    }[name]


def _answer_demographic(page, frame, item, label, answers, blockers):
    if not item.required:
        return
    if item.kind == "combobox":
        item.options = _combobox_options(page, frame, item)
    decline = next((option for option in item.options if _DECLINE.search(option)), None)
    if decline and _set(page, frame, item, [decline]):
        answers.append({"label": label, "answer": decline, "source": "declined"})
    else:
        blockers.append(f'It requires a personal answer that only you should give: "{label}"')


def _answer_checkbox(frame, item, label, job, answers, blockers):
    """A single tick box is a statement in the user's name, so it needs their permission."""
    if not item.required:
        if item.value == "checked" and _CONSENT.search(item.identity):  # pre-ticked marketing and the like
            try:
                frame.locator(f'[data-jf-option="{item.key}:0"]').uncheck(force=True, timeout=ACTION_MS)
            except PlaywrightError:
                blockers.append(f'It has a box ticked for you that you may want to untick: "{label[:120]}"')
        return
    if job.allow_consent and _set(None, frame, item, ["Yes"]):
        answers.append({"label": label, "answer": "Ticked", "source": "your permission"})
    else:
        blockers.append(f'It needs you to agree to: "{label[:120]}"')


def _answer_consent_question(page, frame, item, label, job, answers, blockers):
    """The same kind of agreement, asked as a dropdown or a choice of answers."""
    if not item.required:
        return
    if item.kind == "combobox":
        item.options = _combobox_options(page, frame, item)
    agree = next((option for option in item.options if _AFFIRM.search(option)), None)
    if job.allow_consent and agree and item.kind != "checkboxes" and _set(page, frame, item, [agree]):
        answers.append({"label": label, "answer": agree, "source": "your permission"})
    else:
        blockers.append(f'It needs you to agree to: "{label[:120]}"')


def _attach(frame, item, label, job, folder, answers, blockers):
    """Attaches the CV or cover letter, once each. Returns True if a file went in."""
    done = {answer["answer"] for answer in answers}
    is_resume = _is_my_cv_box(item)
    is_cover = not _RESUME.search(item.identity) and bool(_COVER.search(item.identity))
    if (is_resume and job.resume_name in done) or (is_cover and job.cover_name in done):
        return False  # the same box again: sites rebuild it after an upload
    if is_resume:
        name, data = job.resume_name, job.resume_bytes
    elif is_cover:
        name, data = job.cover_name, job.cover_bytes
    else:
        if item.required:
            blockers.append(f'It wants a file you\'ll need to add yourself: "{label}"')
        return False
    if item.accept and not _accepts(item.accept, name):
        if item.required or is_resume:
            blockers.append(f'It only accepts {item.accept} for "{label}"')
        return False
    path = folder / name
    path.write_bytes(data)
    try:
        frame.locator(f'[data-jf="{item.key}"]').set_input_files(str(path), timeout=ACTION_MS)
    except PlaywrightError:
        if item.required or is_resume:
            blockers.append(f'Couldn\'t attach your file to "{label}"')
        return False
    answers.append({"label": label, "answer": name, "source": "cv" if is_resume else "message"})
    return True


def _accepts(accept, name):
    suffix = Path(name).suffix.lower()
    allowed = [part.strip().lower() for part in accept.split(",")]
    mimes = {".pdf": "pdf", ".docx": "word"}
    return any(part == suffix or part == "*/*" or mimes.get(suffix, "\0") in part for part in allowed)


def _usable_values(item, reply):
    """Gemini's answer, kept only if the rules allow it. An unusable answer means the user is asked."""
    values = [str(value).strip() for value in reply.values if str(value).strip()]
    if reply.source not in ("facts", "cv", "writing") or not values:
        return []
    if item.facts_only and reply.source != "facts":
        return []
    if reply.source == "writing" and item.kind not in ("text", "textarea"):
        return []
    if item.kind == "checkbox":
        return values[:1] if values[0].lower() == "yes" else []
    if item.options:
        matched = [_match_option(item.options, value) for value in values]
        matched = [value for value in matched if value is not None]
        return matched if item.multiple else matched[:1]
    value = values[0]
    if len(value) > (MAX_LONG_ANSWER if item.kind == "textarea" else MAX_TEXT_ANSWER):
        return []
    if item.type == "number" and not re.fullmatch(r"-?\d+(\.\d+)?", value):
        return []
    if item.type == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return []
    if item.type == "url" and not re.match(r"https?://\S+$", value):
        return []
    return [value]


def _match_option(options, value):
    wanted = value.casefold()
    for option in options:
        if option.casefold() == wanted:
            return option
    return None


def _combobox_options(page, frame, item):
    box = frame.locator(f'[data-jf="{item.key}"]').first
    try:
        box.click(timeout=ACTION_MS)
        page.wait_for_timeout(300)
        options = frame.evaluate(_READ_OPTIONS, item.key)
        page.keyboard.press("Escape")
        return options[:MAX_OPTIONS]
    except PlaywrightError:
        return []


def _set(page, frame, item, values):
    """Puts the values into the field. False if the page wouldn't take them."""
    target = frame.locator(f'[data-jf="{item.key}"]').first
    try:
        if item.kind == "select":
            target.select_option(label=values, timeout=ACTION_MS)
        elif item.kind == "combobox":
            return _pick_from_combobox(page, frame, item, target, values[0])
        elif item.kind in ("radio", "checkboxes"):
            for value in values:
                _click_option(frame, item, item.options.index(value))
        elif item.kind == "checkbox":
            _click_option(frame, item, 0)
        else:
            text = values[0]
            if item.maxlength and len(text) > item.maxlength:
                if item.kind != "textarea":
                    return False  # a cut-off email or phone number would be wrong, not just short
                text = text[: item.maxlength]
            if not (item.kind == "text" and _PLACE.search(item.identity) and _pick_place(page, frame, target, text)):
                target.fill(text, timeout=ACTION_MS)
    except PlaywrightError as exc:
        log.info("Couldn't fill %r: %s", item.label, exc)
        return False
    return True


def _same_place(option, value):
    """Every part of "Karachi, Pakistan" must be there, so "Karachi, Mississippi" and "Romania" for "Oman" aren't."""
    parts = [piece.strip() for piece in value.split(",") if piece.strip()]
    first_segment = option.split(",")[0]
    for index, part in enumerate(parts):
        names = _PLACE_NAMES.get(part.casefold(), re.escape(part))
        if index and index == len(parts) - 1 and len(part) > 3:  # sites shorten countries: "Karachi, Sindh, PAK"
            names += f"|{re.escape(part[:3])}"
        # The first part must lead the option: "United Kingdom" shouldn't pick "Belfast, United Kingdom".
        if not re.search(rf"\b({names})\b", option if index else first_segment, re.I):
            return False
    return True


def _pick_place(page, frame, target, text):
    """Types a place and picks the site's matching suggestion. False if it doesn't offer one."""
    target.fill("", timeout=ACTION_MS)
    target.press_sequentially(text.split(",")[0].strip()[:30], delay=15, timeout=ACTION_MS)
    suggestions = frame.locator(_SUGGESTIONS)
    for _ in range(PLACE_CHECKS):  # the list comes from a search, which can take a few seconds
        page.wait_for_timeout(500)
        for index in range(min(suggestions.count(), 15)):
            suggestion = suggestions.nth(index)
            if suggestion.is_visible() and _same_place(suggestion.inner_text(), text):
                suggestion.click(timeout=ACTION_MS)
                return True
    return False


def _click_option(frame, item, index):
    option = frame.locator(f'[data-jf-option="{item.key}:{index}"]').first
    if item.type == "buttons":
        option.click(timeout=ACTION_MS)
        return
    try:
        option.check(force=True, timeout=ACTION_MS)
    except PlaywrightError:
        option.evaluate("el => el.click()")  # custom widgets hide the real input


def _pick_from_combobox(page, frame, item, box, value):
    box.click(timeout=ACTION_MS)
    box.press_sequentially(value.split(",")[0][:30], delay=15, timeout=ACTION_MS)
    page.wait_for_timeout(600)
    options = frame.evaluate(_READ_OPTIONS, item.key)
    if not options:  # options that come from a search take longer
        page.wait_for_timeout(1500)
        options = frame.evaluate(_READ_OPTIONS, item.key)
    wanted = value.casefold()
    for matches in (lambda text: text.casefold() == wanted, lambda text: not item.options and _same_place(text, value)):
        for index, text in enumerate(options):
            if matches(text):
                frame.locator(f'[data-jf-choice="{index}"]').first.click(timeout=ACTION_MS)
                return True
    page.keyboard.press("Escape")
    return False


def _all_text(page):
    parts = [_safe_evaluate(frame, "() => document.body ? document.body.innerText : ''") or "" for frame in page.frames]
    return "\n".join(parts)


class _Traffic:
    """What the page sends after Submit is pressed. No accepted send means nothing was applied for."""

    def __init__(self, page):
        self.page = page
        self.writes = []
        self.document_status = None
        page.on("response", self._seen)

    def _seen(self, response):
        try:
            request = response.request
            if request.method not in ("GET", "HEAD", "OPTIONS") and not _BACKGROUND_REQUEST.search(request.url):
                self.writes.append(response.status)
            if request.is_navigation_request() and request.frame == self.page.main_frame:
                self.document_status = response.status
        except PlaywrightError:
            pass

    @property
    def accepted(self):
        return any(status < 400 for status in self.writes)


def _await_result(page, before, start_url, traffic):
    """After the one click on Submit. It takes data accepted by the site plus a new confirmation to count as success."""
    had_success = bool(_SUCCESS.search(before))
    had_code = bool(_CODE_PROMPT.search(before))
    for second in range(RESULT_SECONDS):
        page.wait_for_timeout(1000)
        text = _all_text(page)
        if traffic.accepted:
            match = _SUCCESS.search(text)
            if match and not had_success and not _NOT_SUCCESS.search(text[max(0, match.start() - 60): match.start()]):
                return Outcome(SUBMITTED, f'The site confirmed it: "{_around(text, match)}"'), False
            url = urlsplit(page.url)
            if (
                page.url != start_url
                and _SUCCESS_URL.search(url.path)
                and not _BAD_QUERY.search(url.query)
                and (traffic.document_status or 200) < 400
            ):
                return Outcome(SUBMITTED, "The site moved to its confirmation page."), False
        if any(_safe_evaluate(frame, _CAPTCHA, False) for frame in page.frames):
            return Outcome(NEEDS_YOU, CAPTCHA_AFTER_SUBMIT), True
        if _CODE_PROMPT.search(text) and not had_code:
            return Outcome(NEEDS_YOU, CODE_AFTER_SUBMIT), True
        if second >= 3:
            errors = _safe_evaluate(page.main_frame, _FORM_ERRORS) or {"alerts": [], "invalid": 0}
            if errors["alerts"] or errors["invalid"]:
                detail = " ".join(errors["alerts"])[:200] or f'{errors["invalid"]} field(s) were rejected'
                return Outcome(NEEDS_YOU, f"The site didn't accept the form: {detail}. Fix it in the Chrome window."), True
    return Outcome(NEEDS_YOU, UNCONFIRMED_SUBMIT), True


def _around(text, match):
    start = max(0, match.start() - 40)
    return " ".join(text[start: match.end() + 60].split())
