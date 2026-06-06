# Security Policy

## Reporting a vulnerability

Please report security issues **privately** — do not open a public issue for a
vulnerability.

- Preferred: use GitHub's **private vulnerability reporting** on this repository
  (the **Security** tab → *Report a vulnerability*), which opens a draft advisory
  visible only to maintainers.
- We aim to acknowledge a report within a few days and to provide a remediation
  plan or fix timeline once triaged.

When reporting, please include the affected version, reproduction steps, and the
impact you observed.

## Scope

zing is a local-first auditing tool. The security properties that matter most:

- **Credential handling.** API keys are fingerprinted (SHA-256, truncated) and
  never stored verbatim. Reports and logs route every relay-controlled string
  through the redactor (`zing/utils/redact.py`) before serialization. A path that
  lets a configured key or another secret reach a JSON/Markdown/HTML report is an
  in-scope vulnerability.
- **Untrusted input.** Relay responses are untrusted by design. Report renderers
  must neutralize relay-controlled text (HTML-escape, Markdown-escape) so it
  cannot inject markup or spoof report structure.
- **No surprising egress.** zing only contacts the endpoints you configure (the
  target, an optional baseline, and an optional judge). A change that sends data
  elsewhere is in scope.

## Responsible use (not a vulnerability)

zing reports *black-box evidence of divergence and risk*, not proof of fraud. It
cannot prove a provider logs prompts, always routes to one exact model, or
commits billing fraud. Publishing an accusation against a vendor based on a single
run is a misuse of the tool, not a security issue — see the "Responsible use"
section of the README and `docs/METHODOLOGY.md`.
