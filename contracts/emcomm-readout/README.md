# emcomm-readout/1 — the incident read-out contract

The one thing **church-emcomm-wnlk-forms** (the EOC Message Center) and **HAM Sidekick Station** (the station)
share, so net control can read the current incident report on a voice net. Neither project imports the other's
code; either works without the other.

- `emcomm-readout-1.schema.json`: the document (JSON Schema 2020-12).
- `fixtures/*.json`: invented examples (the Juniper Plains example community), each with its correct fingerprint.
  Both repositories keep an identical copy and test against it.

Rules:
- **Facts only, no people.** Counts, unit names, times. Never names of people, phone numbers, email or street
  addresses, GPS, **injuries or missionary status** (owner decision 2026-11-10: not read on an open frequency).
- **Missing is null, never 0.** The voice says "not reported".
- **Fingerprint**: `"sha256:" + sha256(json.dumps(doc_without_generated_at_and_fingerprint, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False).encode("utf-8"))`. An approval binds to it; any change to a fact
  needs a new approval.
- **Approver**: satellite and stake EOCs — the Stake President or his delegate; area — the Area Authority or his
  delegate (owner decision 2026-11-10).
- **Versioning**: a minor change only adds optional fields (consumers ignore unknown fields). A new major is
  `emcomm-readout/2`; a consumer refuses majors it does not know and says so.
- **Transport**: `GET http://<eoc>:8080/api/readout` (the EOC laptop; also on the EOC Wi-Fi), the file
  `readout-latest.json` the EOC writes, or a file carried by hand. Winlink attachment: later.
