# systems-toolkit

A collection of Linux/Python systems-engineering projects, each built to a
consistent standard: fault-tolerant by design, documented properly like production
software, and reviewed like an engineer would review a pull
request.

Every project directory contains the same shape:

- `README.md` — problem, architecture, design decisions, tradeoffs, edge
  cases, limitations, and future work.
- `ENGINEERING_JOURNAL.md` — the actual design process: what was tried,
  what failed, and why the final approach won.
- Source + `tests/` — unit tests covering both normal operation and the
  edge cases called out in the README.

## Projects

| # | Project | Status | Demonstrates |
|---|---|---|---|
| 01 | [Log Analyzer](01-log-analyzer/) | Complete | Shell/Python parsing, fault tolerance |
| 02 | [API Client](02-api-client/) | Complete | Networking, HTTP, error handling |
| 03 | [Web Scraper](03-web-scraper/) | Complete | BeautifulSoup, Requests, data extraction |
| 04 | [PDF Parser](04-pdf-parser/) | Complete | Third-party libraries, document processing |
| 05 | [File Organizer](05-file-organizer/) | Complete | Python fundamentals, file management |
| 06 | [Git Health Checker](06-git-health/) | Complete | Git internals, repository inspection |
| 07 | [SSH Automation](07-ssh-automation/) | Complete | SSH, systems administration |
| 08 | [Corrupted CSV Cleaner](08-corrupted-csv/) | Complete | Data cleaning, defensive programming |
| 09 | [Duplicate Finder](09-duplicate-finder/) | Complete | Hashing, streaming, scale |
| 10 | [AI CLI Assistant](10-ai-cli/) | Complete | Prompt engineering, CLI architecture |

## Working Convention

Each project is completed fully — planning, implementation, tests,
documentation, engineering journal — before the next one starts. See
`01-log-analyzer/ENGINEERING_JOURNAL.md` for the pattern each subsequent
project's journal follows.
