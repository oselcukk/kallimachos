# Sources

Kallimachos discovers new literature through source plugins (`src/kallimachos/sources/`).
Every source talks to the network only through the shared `HttpClient` (`kallimachos.http`),
which applies per-host intervals, `Retry-After`, the cache and the OpenAlex budget.

Google Scholar, MathSciNet and ResearchGate are **not** sources and never will be. Scholar alerts
reach Kallimachos only as e-mails in your own mailbox (`scholar_imap`).

## Built-in sources

| Source | Endpoint | Min. interval | Key | Default | Notes |
|---|---|---|---|---|---|
| `arxiv` | `https://export.arxiv.org/api/query` (falls back to `http://` for the run if https fails) | 3.1 s | none | on | `cat:` OR query per profile categories, `submittedDate` window in GMT; ≤ 2000 per page, `start + max_results ≤ 30000`; aliases `math.MP→math-ph`, `math.IT→cs.IT`; PDF `arxiv.org/pdf/<id>v<n>` |
| `iacr_eprint` | `https://eprint.iacr.org/rss/rss.xml?order=recent`; paper page `citation_*` meta | 1.0 s | none | off | PDF `eprint.iacr.org/<id>.pdf` |
| `crossref` | `https://api.crossref.org/journals/{issn}/works`, `/works/{doi}` | 0.6 s | none; optional `mailto_ref` (polite pool) | on | `mode: full` journals only; cursor paging (`*` → `next-cursor`, stop on a short page); `mailto` is sent to Crossref only |
| `openalex` | `https://api.openalex.org/works` | 0.15 s | optional `api_key_ref`, sent as `Authorization: Bearer` | on | journals, followed authors, seed citations, channel keywords; daily USD budget; `from_publication_date` unless `premium_filters: true` (`from_created_date`) |
| `semantic_scholar` | `https://api.semanticscholar.org/graph/v1/paper/search/bulk`, `/paper/{id}` | 1.2 s | optional `api_key_ref`, sent as `x-api-key` | off | HTTP 429 closes the source for the rest of the run |
| `zbmath` | OAI `https://oai.zbmath.org/v1/` (`ListRecords`, `oai_zb_preview`, set per MSC2); REST `https://api.zbmath.org/v1/document/_search` | 2.5 s | none | off | set name from `oai_set_template` (default `{msc2}`); empty set (`noRecordsMatch`) is silent; a lost `resumptionToken` restarts the set once |
| `pubmed` | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/{esearch,efetch}.fcgi` | 0.35 s (0.1 s with a key) | optional `api_key_ref`; `tool` + `email_ref` | off | NCBI asks you to register `tool` and `email`; large jobs at weekends or 21:00–05:00 US Eastern |
| `biorxiv` | `https://api.biorxiv.org/details/{server}/{from}/{to}/{cursor}/json` | 1.0 s | none | off | `servers: [biorxiv, medrxiv]`; 30 records per call; newest version per DOI |
| `rss` | your feed URLs | 1.0 s | none | off | RSS 2.0, RSS 1.0 (RDF) and Atom via `xml.etree`; `channel_hint` per feed |
| `scholar_imap` | your IMAP mailbox (read-only) | — | `user_ref`, `password_ref` | off | parses alert e-mails from `sender_filter`; Scholar itself is never contacted |

Intervals are floors: `config validate` rejects smaller values. `kallimachos sources list` shows
each source with its interval, hosts and whether its key references resolve (present/absent
only; values are never printed). `kallimachos sources test` tests every enabled source;
`kallimachos sources test NAME` makes one small request to a single source and exits with 6 when
it fails.

## Rules every source follows

* **One HTTP layer.** Call `self.fetch(http, url, params=..., headers=..., kind=...)` (from
  `BaseSource`). Never open sockets, never build your own session, never set `User-Agent`.
* **Request kinds.** `single` (one entity, cached 30 days), `list` (filter queries), `search`
  (full-text search). OpenAlex prices calls by kind.
* **Secrets.** Read them with `self.secret(config.<x>_ref, "<label>")`. Send them only in the
  header the provider documents (or in `params` when the provider has no header, e.g. PubMed's
  `api_key`), never formatted into a URL, a record, an error or a warning. A missing secret is a
  warning and the source continues without it.
* **Failures do not stop a run.** `kallimachos.sources.base.run_source` wraps `discover`: HTTP
  errors, `self.error(...)` messages and even exceptions end up in `SourceStats.errors`. Expected
  HTTP errors (for example zbMATH's empty-set 400) are passed to `self.silence(result)`.
* **Data is not instructions.** Titles, abstracts and e-mail bodies are stored as text only.
* **Records.** Fill what the source knows: `title`, `authors`, `abstract` + `abstract_source`,
  identifiers (`doi` lower-case without `https://doi.org/`, `arxiv_id` without version, ...),
  `published`, `created`, `url`, `kind`, `sources={"<name>"}` and `queries` (which query found
  it, e.g. `openalex:journal_full`). The pool merges records from several sources.

## Adding a source

A new source is one module, its fixtures and a registry entry.

1. **Module** `src/kallimachos/sources/<name>.py`:

   ```python
   class ExampleSource(BaseSource):
       name = "example"
       needs_key = False
       default_interval_s = 1.0
       hosts = ("api.example.org",)

       def __init__(self, config: object, *, resolve_secret: SecretResolver | None = None) -> None:
           super().__init__(config, resolve_secret=resolve_secret)
           self.config = config  # your config dataclass

       def discover(self, window, profile, http): ...      # yield Record
       def metadata(self, ident, http): ...                # Record | None
       def oa_pdf_candidates(self, record, http): ...      # list[OaCandidate], open access only
       def test(self, http): ...                           # one small request -> SourceTest
   ```

   `discover` must respect the half-open UTC `window` and stop paging on the provider's documented
   limits. `oa_pdf_candidates` proposes only legal open-access locations (no publisher
   authentication, no shadow libraries).

2. **Fixtures** `tests/fixtures/<name>/`: one `<case>.json` (request, response status/headers,
   `body_file`, `expect`) and one `<case>.body.<ext>` per recorded exchange (see
   `tests/fixtures/README.md`). Record at most a couple of real, key-less, read-only responses,
   shrink them to a few records and replace every person name, title, abstract and identifier with
   fictional values (`Example Author A`, `10.0000/example.1`). Strip `Authorization`/`x-api-key`.

3. **Tests** `tests/unit/test_sources_<name>.py`: replay the fixtures through
   `tests/unit/test_sources_fakes.FakeHttp` and cover `discover`, `metadata`,
   `oa_pdf_candidates` and `test`, plus paging and error paths. Add the source to
   `tests/unit/test_sources_leaks.py`. Tests never touch the network.

4. **Registry.** Built-in sources are listed in `sources/registry.py` (`BUILTIN_NAMES` and
   `_BUILTIN_CLASSES`) and need a config section in `config/schema.py` plus an interval floor in
   `DEFAULT_MIN_INTERVAL_S` (both owned by the project lead; ask for them). A third-party package
   can instead expose its class through the `kallimachos.sources` entry-point group:

   ```toml
   [project.entry-points."kallimachos.sources"]
   example = "example_package.source:ExampleSource"
   ```

   Third-party sources show up in `sources list` and `sources test`; they cannot replace a
   built-in name. Because the configuration shape is fixed, a third-party source receives `None`
   as its config and is not enabled by `config.yaml`.

5. **Docs.** Add a row to the table above.

## `archive test`

`kallimachos archive test` makes one small request to the first enabled open-access source the
archive downloads from (arXiv, then IACR ePrint, OpenAlex, Crossref) and checks that
`archive.root` is set and writable. Exit 6 when the request fails, 3 when only the root check
fails, 2 when no such source is enabled.
