Transcribe speech faithfully in its original language. Do not translate the transcript.
In SPEAKERS_JSON, use short, unique, readable speaker keys instead of S1/S2:
- Use only the speaker's short first name when distinct (e.g. Сергей, Лена, Антон, Олег) —
  never put a full surname, patronymic, or contact tag in the speaker key when the first name
  alone is unambiguous (use Антон instead of Антон Павлович Имморталист). Prioritize:
  (1) the name spoken/heard in the recording, (2) a real personal first name from the filename
  contact metadata (to reflect phonebook renames, ignoring technical tags/nicknames like
  "Муж GSM", "Покупатель 1.6TB", "Владелец"), and (3) the catalog `name` in people.yaml /
  orgs.yaml (keep the stable catalog `id`).
- Only if two speakers in the same recording share a first name, distinguish the non-owner /
  second speaker by surname or full name (e.g. Сергей vs Сергей Смирнов, Антон vs
  Соколов / Соколов Антон, or Имя 2 if surname is unknown).
- If no name is known, use the shortest readable role that distinguishes the voice.
  Prefer one word (e.g. Оператор, Врач, Курьер, Робот, Собеседник). Use Сотрудник
  instead of Сотрудник центра поддержки. Add a second word or a number only to
  distinguish voices when needed (e.g. Голос 1, Голос 2).
- Never use "?" or "unknown" as a key in SPEAKERS_JSON. Use **?** in utterance lines
  only when an individual utterance cannot be reliably assigned to any audible voice.
One voice means just one speaker key, except that a known phone owner is also a call
participant when only a robot/voicemail speaks. Include that silent owner in SPEAKERS_JSON
with person: <owner>, role: "owner", source: "filename", and
note: "Владелец телефона; без слышимых реплик". Do not invent any utterances for them.
The called contact is not the robot's person identity; a voicemail greeting does not
mean the called person spoke. For a recording with no speech at all, use NO_SPEECH.
Use [неразборчиво] for unintelligible speech and NO_SPEECH when no speech is audible.

Consider the identity of EVERY speaker in SPEAKERS_JSON: self-introductions, forms of address, context.
For each speaker, set:
- person: an existing catalog key from people.yaml (e.g. ivanov_sergey), or a newly
  created GOST 7.79-2000 ID when a named non-temporary person (especially from filename
  contact metadata or clear full identification in speech) is not yet in people.yaml,
  or null when not identified. Omit person for an organization robot/IVR.
  Rules for creating a new ID (lowercase ASCII [a-z0-9_] only, GOST 7.79-2000 System B:
  а=a,б=b,в=v,г=g,д=d,е=e,ё=yo,ж=zh,з=z,и=i,й=j,к=k,л=l,м=m,н=n,о=o,п=p,р=r,с=s,т=t,
  у=u,ф=f,х=kh,ц=c,ч=ch,ш=sh,щ=shh,ъ="",ы=y,ь="",э=eh,ю=yu,я=ya):
  1) First check people.yaml (including `aliases` and `nicknames`) and `other_people_keys`
     (where known nicknames appear in brackets, e.g. `Name [Nick1, Nick2]`) so you recognize
     people addressed by nickname in speech and never duplicate an existing ID.
  2) Format: <surname>_<firstname> if surname and first name are known (e.g. kuznetsov_andrey,
     petrov_ivan), or <firstname>_<role_or_tag> from the contact (e.g. andrey_sborka_kukhni).
     Put the full readable name in label (never invent an unknown surname).
  3) Do NOT create a new ID for temp: true strangers or generic unnamed roles.
- org: for an organization robot/IVR (`role: "robot"`, even with `temp: true`), set `org`
  to an existing key from `orgs.yaml` / `other_orgs_keys` — or, when the company/service is
  clearly named in speech or contact metadata (e.g. Билайн, Сбербанк, Почта России) and not
  yet listed, set a concise GOST 7.79-2000 org key (e.g. `beeline`, `sberbank`) AND set `label`
  to its readable Russian name (e.g. `Билайн`) so it can be auto-registered once; set `source`
  to `"speech"` or `"filename"`. Leave `org` omitted if the organization is unknown.
- source: "speech" (confirmed by what was said or by an approved voice reference),
  "filename" (established from filename/phone metadata or folder owner when the
  audio does not contradict it), or "user" (when explicitly given).
  Omit source if neither person nor org is linked.
- evidence: for a linked person or robot organization with source: "speech",
  use exactly one of the following, in this order for each speaker:
  1) "name" when an audible name or nickname clearly identifies that voice
     (or a robot's organization names itself). A third-party mention alone does
     not link the name to a voice.
  2) "voice_sample" for a clear match to the approved reference supplied for this
     recording when no reliable spoken name identifies the voice.
  3) "context" when what is said identifies the speaker and neither of the above
     applies. Briefly state the clue in note. A role, contact name in the filename,
     call direction, or folder owner alone is not evidence from speech.
  Omit evidence for other sources and unlinked identities.
- temp: true ONLY for clearly one-off/transient strangers (courier, call-center operator,
  robot, taxi dispatcher, random wrong number) who do not need a catalog card.
- nicknames: optional list of strings, ONLY for an identified non-temporary person
  (`person` is set and not `temp: true`) when speech clearly addresses or refers to them
  by a distinctive, non-standard nickname/moniker not already in their `name` or `nicknames`
  (use nominative/base form when clear, e.g. `["Кузнец"]`). NEVER include standard
  diminutives/forms of their first name (e.g. Вова, Серёжа, Лена, Катя, Саша, Дима, Миша),
  generic/affectionate/kinship terms (брат, дружище, солнышко, зай, котик, мама, папа,
  шеф, дорогой, мужик, чувак), or phonebook tags. Omit if none.
- note: optional when `person` or `org` is already linked (omit for straightforward `filename`
  or `owner` matches; add a brief 2–5 word note only if useful, e.g. `Представился (00:00:18)`).
  REQUIRED only when `person: null` and no `org` is linked — keep it ultra-concise (3–6 Russian
  words in one phrase, e.g. `Автоответчик, оператор не назван`, `Не представился`,
  `Только имя в речи`, `Фоновый шёпот`; never repeat `summary`, file paths, or obvious context).
If a name/surname is audible and no new ID is appropriate (e.g. temp: true or only a
first name without contact surname/context), keep person: null and label if helpful,
explaining in note. Never invent surnames, facts, evidence, or observations.
A matching contact or phone number in the filename is sufficient to identify that non-temporary
participant (`source: "filename"`), unless the conversation indicates that someone else is using
their phone (if contradicted by speech, set `person: null` or the actual person and explain in
`note`). An operator's organization does not identify that human; only a robot may have
`role: "robot"` + `org` link (with `source: "speech"` or `"filename"`).
On `type: phone` calls (when a folder owner like ivanov_sergey is given and
the conversation does not indicate someone else is using the phone), identify the device user
(caller in outgoing — including calls that reach voicemail/answering machine — and answerer in
incoming) by their readable first name (e.g. Сергей, Елена) with person: <owner> — use
source: "speech" if speech/context also confirms it, or source: "filename" otherwise
(`note` may be omitted); that is sufficient for identification. If speech shows someone else is using the phone,
do NOT assign the owner's `person` ID. Never leave the phone owner under generic labels like
Звонящий, Абонент, Владелец телефона, Ответивший, Собеседник, or Мужчина. Write note and summaries in Russian; preserve names as heard.

After SPEAKERS_JSON, output SUMMARY_JSON: followed by a valid JSON string containing
at most 20 whitespace-separated words. Describe the subject, outcome, or agreement.
Avoid generic filler and do not invent facts. Output SUMMARY_JSON: null if no useful
summary can be established. Do not estimate spam_score in this task; it remains null.
Short batches and long recordings must provide the same metadata and transcript format.

Timestamp each utterance onset as [HH:MM:SS.cc] **<Speaker>** text (or [HH:MM:SS.cc] **?** text),
without a colon inside or after the bold speaker name, and with TWO fractional digits. Listen for actual onsets, including rapid speaker changes, and give
the best estimate supported by the audio. If only tenths are supported, pad with a final zero.
A trailing zero is acceptable. Never invent nonzero final digits to look precise.
Formatting alone does not prove hundredth-second accuracy. Times must be nondecreasing and
within the supplied duration. Preserve meaningful repetitions, qualifications, and negations.
