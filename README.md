# unduhin

> **Bilingual documentation:** [English](#english) | [Bahasa Indonesia](#bahasa-indonesia)

---

## English

Local, offline-first CLI and minimal localhost web UI for converting media URLs to **MP3 (audio)** or **MP4 (video)** on your own computer—built to replace ad-heavy online converter websites without cloud uploads, accounts, or telemetry.

> **Legal Notice:** Use `unduhin` only for content you own, hold a license for, or have explicit permission from the rights holder to download. Downloading copyrighted content without permission may violate applicable law or platform terms.

> **License:** [MIT License](LICENSE)

---

### Features

- **Audio (`mp3`)** and **Video (`mp4`)** conversion powered in-process by `yt-dlp` (`yt_dlp.YoutubeDL`) and `FFmpeg` (H.264 + AAC prioritized for MP4 compatibility, with a warning if codec fallback is used).
- **Zero-friction CLI:** run `unduhin "<URL>"` directly (shortcut for `download`; place flags after the URL), run `unduhin` with no arguments for quick examples, and omit `--mode` in an interactive terminal to choose `1 = MP3` or `2 = MP4`.
- **Playlists disabled by default** (`--playlist` to opt in, bounded by `--max-items` default `50` / cap `200`, plus global cap `500` items per invocation).
- **Batch mode** via UTF-8 text file (`--batch urls.txt`, comments `#`, blank lines ignored, preserves order, deduplicates, fail-fast validation before touching disk).
- **Filename & path safety:** allowlisted template tokens (`{title}`, `{id}`, `{uploader}`, `{upload_date}`, `{ext}`) sanitized per OS, final path auto-clamped to `<= 240` chars, non-UTF-8 console replacement safety, staged inside `.unduhin-tmp-*`, and moved cleanly with `--on-conflict rename|skip|overwrite`.
- **Two-tier diagnostics (`unduhin doctor`):** separates **Required** tools (`Python >= 3.10`, `yt-dlp`, `FFmpeg`) from **Recommended** YouTube extras (`yt-dlp-ejs`, `Deno >= 2.3.0`), plus **no-download inspection (`unduhin info`, `--dry-run`)**.
- **Minimal localhost web UI (`unduhin ui`)** strictly bound to `127.0.0.1` with EN/ID toggle, System/Light/Dark flat accessible theme, cooperative Cancel button, and fixed launch-time output directory.

### Non-Goals

By design, `unduhin` will **never** implement:
- Accounts, logins, or cookie extraction / browser profile reading
- DRM circumvention
- Mass scraping or crawling
- Public / cloud / multi-tenant server hosting
- Silent auto-updates or any telemetry / analytics

---

### Installation

#### 1. System Prerequisites (`FFmpeg` required; `Deno` recommended for YouTube)

`FFmpeg` is required to convert MP3s and merge MP4 streams. `Deno >= 2.3.0` (paired with `yt-dlp-ejs` from `yt-dlp[default]`) is **only required for YouTube downloads**; non-YouTube sites can run without Deno.

| Platform | FFmpeg ([ffmpeg.org](https://ffmpeg.org/download.html)) *(Required)* | Deno ([deno.com](https://deno.com)) *(Recommended for YouTube)* |
| :--- | :--- | :--- |
| **Windows** | `winget install Gyan.FFmpeg` *(restart terminal afterward)* | `winget install DenoLand.Deno` or `irm https://deno.land/install.ps1 \| iex` |
| **macOS** | `brew install ffmpeg` | `brew install deno` |
| **Linux** | `sudo apt install ffmpeg` *(or `dnf`/`pacman`)* | `curl -fsSL https://deno.land/install.sh \| sh` *(add `~/.deno/bin` to `PATH`)* |

- Official `yt-dlp` project: [github.com/yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) (installed via `yt-dlp[default]`).

#### 2. Quick Install `unduhin` (Python >= 3.10)

Install from local source / repository:

```bash
# From current cloned repository directory (CLI only: 25 packages, ~32.7 MB):
uv tool install .
# Or with optional Gradio localhost web UI (63 packages, ~219.7 MB):
uv tool install ".[ui]"

# From built wheel package:
uv tool install ./dist/unduhin-0.1.0-py3-none-any.whl

# Alternatively using pipx or pip:
pipx install .
pip install .
pip install ".[ui]"
```

*(Note: Once published to PyPI, public one-command install `uv tool install unduhin` will be available).*

Upgrade `yt-dlp` later according to how you installed `unduhin`:

```bash
# When installed via uv tool:
uv tool upgrade unduhin               # (available after public PyPI release)
# In development / local wheel / pip environment:
pip install -U "yt-dlp[default]"
pipx runpip unduhin install -U "yt-dlp[default]"
```

Verify your environment:

```bash
unduhin doctor
```

---

### Quick Start (CLI)

```bash
# Show quick examples and doctor/help hints
unduhin

# Check Required (Python, yt-dlp, FFmpeg) & Recommended (yt-dlp-ejs, Deno)
unduhin doctor

# Shortcut syntax: download directly without typing 'download'
# Interactive mode prompt asks (1 = MP3, 2 = MP4) if --mode is omitted in a terminal
unduhin "https://example.com/watch?v=VIDEO_ID"

# Explicit MP3 (192 kbps) into default ~/Downloads/unduhin
unduhin "https://example.com/watch?v=VIDEO_ID" --mode audio --bitrate 192

# Explicit MP4 capped at 1080p into custom folder
unduhin download "https://example.com/watch?v=VIDEO_ID" --mode video --max-height 1080 -o ./out

# Inspect metadata or preview filenames (--dry-run) without downloading
unduhin info "https://example.com/watch?v=VIDEO_ID"
unduhin download "https://example.com/watch?v=VIDEO_ID" --mode audio --dry-run

# Batch download from UTF-8 URL list
unduhin download --batch urls.txt --mode audio

# Explicitly enable playlist download (disabled by default)
unduhin download "https://example.com/playlist?list=PL_ID" --mode audio --playlist --max-items 10

# Launch minimal localhost web UI on http://127.0.0.1:7860
unduhin ui --port 7860
```

---

### Environment Variables & CLI Flags

Only two environment variables are used for configuration:
- `UNDUHIN_OUTPUT_DIR`: overrides default output folder `~/Downloads/unduhin` (`--output-dir` still wins over `UNDUHIN_OUTPUT_DIR`).
- `UNDUHIN_LANG`: overrides OS locale language detection (`en` or `id`; `--lang` still wins over `UNDUHIN_LANG`).

#### `unduhin [download] [URL]`

*(When using shortcut `unduhin <URL> [OPTIONS]`, place all options **after** the URL.)*

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--batch` | `-B` | `None` | UTF-8 text file of URLs (`#` comments, blank lines ignored; max 1 MB, 500 URLs). |
| `--mode` | `-m` | prompt / required | Target format: `audio` (MP3) or `video` (MP4). Prompts `1/2` in a terminal if omitted; non-TTY scripts must pass `--mode` (else exit `2`). |
| `--bitrate` | `-b` | `192` | MP3 audio bitrate in kbps (`64`–`320`). |
| `--max-height` | `-H` | `1080` | Maximum MP4 video height in pixels (e.g. `720`, `1080`). |
| `--output-dir` | `-o` | `~/Downloads/unduhin` | Destination folder (`--output-dir` > `UNDUHIN_OUTPUT_DIR` > `~/Downloads/unduhin`; symlinks rejected; printed cleanly in summary). |
| `--template` | `-t` | `{title}.{ext}` | Template using `{title}`, `{id}`, `{uploader}`, `{upload_date}`, `{ext}` (clamped so final path `<= 240` chars). |
| `--on-conflict` | | `rename` | Conflict policy: `rename`, `skip`, or `overwrite`. Intra-batch title duplicates still suffix `(N)`. |
| `--playlist` | | `false` | Opt in to playlist expansion. |
| `--max-items` | | `50` | Max playlist items per playlist (`1`–`200`; global batch cap is `500`). |
| `--dry-run` | | `false` | Inspect & resolve filenames without downloading or creating output files. |
| `--lang` | | `UNDUHIN_LANG` / locale | UI/message language override: `en` or `id`. |
| `--verbose` | `-v` | `false` | Show detailed `yt-dlp` diagnostic messages and unexpected-error tracebacks. |

#### `unduhin info URL`

Supports `--mode/-m`, `--bitrate/-b`, `--max-height/-H`, `--playlist`, `--max-items`, `--lang`, `--verbose/-v`.

#### `unduhin doctor`

Supports `--lang`, `--verbose/-v`.

#### `unduhin ui`

| Flag | Short | Default | Description |
| :--- | :--- | :--- | :--- |
| `--output-dir` | `-o` | `~/Downloads/unduhin` | Fixed destination directory (`--output-dir` > `UNDUHIN_OUTPUT_DIR` > `~/Downloads/unduhin`; cannot be edited in the browser). |
| `--port` | `-p` | `7860` | Local TCP port (`1`–`65535`). Always binds to `127.0.0.1` (`--host` is intentionally absent). |
| `--lang` | `-l` | `UNDUHIN_LANG` / locale | Initial UI language: `en` or `id`. |
| `--verbose` | `-v` | `false` | Show detailed tracebacks on unexpected errors. |

---

### Exit Codes

Precedence when multiple conditions occur: `130 > 3 > 2 > 1 > 0`.

| Code | Constant | Meaning |
| :--- | :--- | :--- |
| `0` | `SUCCESS` | All items succeeded or were cleanly skipped as already existing (`SKIPPED_EXISTS`). `unduhin doctor` also returns `0` when all Required tools are present even if Recommended YouTube extras (`Deno`/`yt-dlp-ejs`) warn. |
| `1` | `FAILURE` | At least one item failed (`FAILED`) or was unsupported (`SKIPPED_UNSUPPORTED`), UI port busy, unexpected error, or batch halted by the HTTP 429 circuit breaker. |
| `2` | `INVALID_INPUT` | Invalid URL scheme/host, invalid template/path (> 240 chars headroom), omitted `--mode` in non-TTY, invalid port, or invalid/unreadable `--batch` file (fail-fast before touching disk). |
| `3` | `MISSING_DEPENDENCY` | Missing required dependency (`Python`/`yt-dlp`/`FFmpeg` in `doctor`, `FFmpeg` during download, `Deno` during YouTube download, or `gradio` for `unduhin ui`). |
| `130` | `CANCELLED` | Interrupted by user (`Ctrl+C` / `SIGINT`). |

---

### Footprint & Startup Benchmarks (Measured on Windows / Python 3.13.14)

- **Lazy CLI startup** (`yt_dlp`, `gradio`, and `rich` are deferred from `--version`, `--help`, empty-args, `doctor`, and `ui --help` until needed):
  - `unduhin --version`: `~0.358s` median (5 runs; reduced from `0.457s`)
  - `unduhin --help`: `~0.337s` median (5 runs; reduced from `0.575s`)
  - Python `import unduhin.cli`: `133.8 ms` cumulative (`-45%` vs `243.8 ms` before)
- **Clean venv install footprint (`uv` build):**
  - Base `unduhin` wheel: `25` packages, `32.67 MB` in `site-packages` (top dep: `yt_dlp` `10.24 MB`)
  - With UI extra `unduhin[ui]`: `63` packages, `219.74 MB` in `site-packages` (adds `gradio` `72.95 MB`, `numpy` `40.08 MB`, `pandas` `37.26 MB`, `pillow` `14.07 MB`)

---

### Web UI (`unduhin ui`)

Install the UI extra with `pip install "unduhin[ui]"` (`gradio>=6.0.0,<7.0.0`).

- **Minimal single-URL scope:** accepts one `http://` or `https://` URL (max `2048` characters), MP3/MP4 selection, Language switcher (`EN` / `ID`), 3-way Theme toggle (`System` / `Light` / `Dark` saved in `localStorage` with `try/catch`), Download button (single-flight queue, `concurrency_limit=1`), and cooperative Cancel button (interrupts worker & removes `.unduhin-tmp-*` staging folders).
- **Playlists and batch files** are intentionally rejected in UI mode; use the CLI for `--playlist` or `--batch`.
- **Output directory is read-only** in the browser; change it only at launch via `unduhin ui --output-dir <path>`.

---

### Privacy & Security Guarantees

- **Strict URL scheme & safety:** accepts `http://` and `https://` only; rejects `file://`, `javascript:`, `data:`, non-IPv4/6 local shenanigans; never uses `shell=True`, `eval`, or `exec`.
- **Path containment & extension whitelist:** staged downloads inside `.unduhin-tmp-*` verify final extension matches `.mp3` or `.mp4` before linking/moving into the validated destination directory. Symlink destinations and parent traversals are rejected.
- **Zero telemetry & localhost isolation (Gradio 6.31.0 audit):**
  - `GRADIO_ANALYTICS_ENABLED="False"` and `HF_HUB_DISABLE_TELEMETRY="True"` are set **before** importing `gradio`.
  - `Blocks(analytics_enabled=False)` and `launch(server_name="127.0.0.1", share=False, inbrowser=False, show_error=False, footer_links=[], run_history=False, allowed_paths=[], enable_monitoring=False)`, with every UI handler marked `api_visibility="private"` and zero `gr.File` components.
  - Uses local system fonts (`system-ui` / `ui-monospace`); zero `GoogleFont`, zero external CSS/JS CDN links.
  - *Audit note for `gradio==6.31.0`:* In our source audit of `gradio==6.31.0` (`Index-Akn07EZk2.js:2,4`), Google Fonts injection (`ge()`) is only invoked by `Ee()` when `config.space_id` is truthy on a Hugging Face Space page, not during local `Blocks.launch()`. Bundled `index.html` retains static OpenGraph `<meta>` `og:image` tags pointing to `raw.githubusercontent.com` (browsers do not fetch meta social images on page navigation). Our subprocess socket-hook test (`socket.socket.connect` rejecting non-loopback targets) also verified zero non-loopback Python connections when starting `unduhin ui` and serving `/` and its local JS/CSS assets (note: Python socket hooks do not verify browser-side JS execution; see [`M17`](docs/MANUAL_TEST.md) for manual browser DevTools checks).

---

### Troubleshooting

- **Missing binary / outdated tools:** Run `unduhin doctor`. If `yt-dlp` is older than 90 days, update via `uv tool upgrade unduhin`, `pipx runpip unduhin install -U "yt-dlp[default]"`, or `pip install -U "yt-dlp[default]"`.
- **HTTP 429 Rate Limit / Slowdown:** `unduhin` retries transient network/rate-limit errors with exponential backoff capped at **120 seconds total sleep per item**. If **3 consecutive items** fail with HTTP 429, the batch circuit breaker halts remaining items and exits `1` so your IP is not hammered. Wait before retrying.
- **"Sign in to confirm you're not a bot":** Because `unduhin` intentionally does not handle logins or export browser cookies, it cannot bypass bot-verification prompts. Wait and try again later, switch to another network/IP, or update `yt-dlp`.
- **Age-restricted / Geo-blocked / Members-only / DRM / Live:** `unduhin` reports the localized reason and skips/fails safely; private/DRM/members-only content cannot be downloaded.
- **Need deeper logs:** Re-run with `--verbose` (CLI) to see underlying `yt-dlp` diagnostic lines.

---

### Development

```bash
uv sync --extra ui --extra dev

# Unit & security tests (mocked media adapter; skips @pytest.mark.network/@pytest.mark.integration)
.venv/bin/pytest   # or .venv\Scripts\pytest.exe on Windows

# Optional live network / integration tests (requires network, FFmpeg, Deno)
pytest -m "network or integration"

# Linting and strict type checking
ruff check src tests
mypy src/unduhin
```

### Tested Matrix (Honest Verification Status)

| Platform / Python | CLI + Pipeline + Security + Lazy-Import Tests (Mocked `FakeMediaAdapter`) | Subprocess UI Localhost Server + Assets Zero Non-Loopback Connections + WCAG | Live End-to-End Downloads (`FFmpeg` + `Deno` + Network) | Real Browser Visual & Network Inspection (Playwright / Browser DevTools) |
| :--- | :--- | :--- | :--- | :--- |
| **Windows / Python 3.13.14** | Verified (`198 passed, 1 skipped` Unix symlink test, `2 deselected` network/integration) | Verified in live subprocess with `socket.socket.connect` guard | **Verified on Windows** with Gyan.FFmpeg 9.0.2 + Deno 2.9.8 + yt-dlp 2026.10.26 (MP3/MP4, generic MP3/MP4-fallback, skip/rename/overwrite, SIGBREAK cancel, 2 network/integration tests passed) | **Not verified** (no headless browser/Playwright installed; manual checklist `M17`) |
| **Linux / macOS / Python 3.10–3.12 (`.github/workflows/ci.yml`)** | Defined in CI workflow — **Not yet run / BELUM diverifikasi** | Defined in CI workflow — **Not yet run / BELUM diverifikasi** | **Not verified** | **Not verified** |

---

## Bahasa Indonesia

Alat **CLI** dan **UI web lokal** yang berjalan sepenuhnya di komputer Anda untuk mengunduh media dari URL menjadi **MP3 (audio)** atau **MP4 (video)**—dirancang sebagai pengganti situs converter online yang penuh iklan berbahaya, tanpa cloud, tanpa akun, dan tanpa telemetri.

> **Pemberitahuan Hukum:** Gunakan `unduhin` hanya untuk konten yang Anda miliki sendiri, berlisensi bebas, atau memiliki izin eksplisit dari pemegang hak cipta.

> **Lisensi:** [Lisensi MIT](LICENSE)

---

### Fitur Utama

- **Audio (`mp3`)** dan **Video (`mp4`)** diproses langsung di Python lewat library `yt-dlp` dan `FFmpeg` (memprioritaskan H.264 + AAC untuk kompatibilitas MP4, dengan peringatan bila terjadi fallback codec).
- **CLI mudah dipakai siapa saja:** jalankan `unduhin "<URL>"` langsung tanpa mengetik `download` (taruh opsi setelah URL), jalankan `unduhin` tanpa argumen untuk bantuan cepat, atau kosongkan `--mode` di terminal untuk memilih `1 = MP3` atau `2 = MP4`.
- **Playlist nonaktif secara default** (aktifkan via `--playlist`, batas per playlist `--max-items` default `50` / maks `200`, dan batas global `500` item).
- **Unduhan massal dari daftar teks UTF-8** (`--batch urls.txt`, komentar `#`, baris kosong diabaikan, menjaga urutan & menghapus duplikat, validasi fail-fast sebelum menyentuh disk).
- **Keamanan nama file, path, & konsol:** token template terbatas (`{title}`, `{id}`, `{uploader}`, `{upload_date}`, `{ext}`), panjang path total dibatasi `<= 240` karakter, aman untuk konsol non-UTF-8, staging `.unduhin-tmp-*`, serta kebijakan `--on-conflict rename|skip|overwrite`.
- **Diagnostik 2-tingkat (`unduhin doctor`):** memisahkan komponen **Wajib** (`Python >= 3.10`, `yt-dlp`, `FFmpeg`) dan **Disarankan untuk YouTube** (`yt-dlp-ejs`, `Deno`), ditambah inspeksi tanpa mengunduh (`unduhin info`, `--dry-run`).
- **UI web lokal minimal (`unduhin ui`)** khusus di `127.0.0.1` dengan pilihan bahasa `EN`/`ID`, tema datar `System`/`Light`/`Dark`, tombol Batal kooperatif, dan folder output tetap saat peluncuran (`~/Downloads/unduhin` atau `UNDUHIN_OUTPUT_DIR`).

### Batasan (Non-Goals)

Sesuai desain, `unduhin` **tidak** akan memiliki: dukungan login/akun/ekspor cookies, bypass DRM, scraping massal, server publik/cloud, pembaruan otomatis diam-diam, maupun telemetri apa pun.

---

### Pemasangan

#### 1. Prasyarat Sistem (`FFmpeg` & `Deno`)

- **Windows:**
  - FFmpeg: `winget install Gyan.FFmpeg` *(restart terminal setelah pemasangan)* — sumber resmi: [ffmpeg.org/download.html](https://ffmpeg.org/download.html)
  - Deno (`>= 2.3.0`, runtime JS resmi untuk YouTube): `winget install DenoLand.Deno` atau `irm https://deno.land/install.ps1 | iex` — sumber resmi: [deno.com](https://deno.com)
- **macOS:** `brew install ffmpeg` dan `brew install deno`
- **Linux:** `sudo apt install ffmpeg` *(atau `dnf`/`pacman`)* dan `curl -fsSL https://deno.land/install.sh | sh` *(pastikan `~/.deno/bin` ada di `PATH`)*
- Sumber resmi `yt-dlp`: [github.com/yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp).

#### 2. Pasang Cepat `unduhin` (Python >= 3.10)

Pasang dari kode sumber / folder lokal:

```bash
# Dari folder repositori lokal (CLI saja: 25 paket, ~32.7 MB):
uv tool install .
# Dengan UI web lokal (Gradio >= 6.0.0,<7.0.0; 63 paket, ~219.7 MB):
uv tool install ".[ui]"

# Dari paket wheel:
uv tool install ./dist/unduhin-0.1.0-py3-none-any.whl

# Atau dari pip / pipx:
pipx install .
pip install .
pip install ".[ui]"
```

*(Catatan: Setelah rilis publik ke PyPI, perintah satu langkah `uv tool install unduhin` akan langsung aktif).*

Cek kesiapan sistem:

```bash
unduhin doctor
```

---

### Contoh Cepat (CLI)

```bash
unduhin
unduhin doctor
unduhin "https://example.com/watch?v=VIDEO_ID"
unduhin "https://example.com/watch?v=VIDEO_ID" --mode audio --bitrate 192
unduhin download "https://example.com/watch?v=VIDEO_ID" --mode video --max-height 1080 -o ./out
unduhin info "https://example.com/watch?v=VIDEO_ID"
unduhin download "https://example.com/watch?v=VIDEO_ID" --mode audio --dry-run
unduhin download --batch urls.txt --mode audio
unduhin download "https://example.com/playlist?list=PL_ID" --mode audio --playlist --max-items 10
unduhin ui --port 7860
```

Variabel lingkungan yang didukung hanya `UNDUHIN_OUTPUT_DIR` (menimpa default `~/Downloads/unduhin`, dikalahkan `--output-dir`) dan `UNDUHIN_LANG` (`en`|`id`, dikalahkan `--lang`).

---

### Tabel Flag Ringkas

- `unduhin [download] [URL]`: `--batch/-B`, `--mode/-m` (`audio`|`video`; tanya interaktif `1/2` bila di terminal, error exit `2` bila non-TTY), `--bitrate/-b` (`64`–`320`), `--max-height/-H`, `--output-dir/-o` *(default `~/Downloads/unduhin`)*, `--template/-t`, `--on-conflict` (`rename`|`skip`|`overwrite`), `--playlist`, `--max-items` (`1`–`200`), `--dry-run`, `--lang` (`en`|`id`), `--verbose/-v`.
- `unduhin info URL`: `--mode/-m`, `--bitrate/-b`, `--max-height/-H`, `--playlist`, `--max-items`, `--lang`, `--verbose/-v`.
- `unduhin doctor`: `--lang`, `--verbose/-v`.
- `unduhin ui`: `--output-dir/-o` *(read-only di browser)*, `--port/-p` (`1`–`65535`), `--lang/-l`, `--verbose/-v`. *(Tidak ada flag `--host`; selalu bind `127.0.0.1`.)*

---

### Arti Exit Code

Urutan prioritas: `130 > 3 > 2 > 1 > 0`.

- `0`: Semua item berhasil atau hanya dilewati karena file sudah ada (`SKIPPED_EXISTS`). `unduhin doctor` juga exit `0` jika komponen Wajib lengkap walau komponen Disarankan (`Deno`/`yt-dlp-ejs`) berstatus `PERINGATAN`.
- `1`: Minimal 1 item gagal (`FAILED`), konten tidak didukung (`SKIPPED_UNSUPPORTED`), port UI terpakai, galat tak terduga, atau batch dihentikan pemutus sirkuit HTTP 429.
- `2`: Input tidak valid (URL non-`http/https`, template/path melebihi batas aman 240 karakter, `--mode` kosong di non-TTY, port salah, atau file `--batch` invalid/gagal dibaca — berhenti sebelum menyentuh disk).
- `3`: Dependensi wajib tidak ditemukan (`Python`/`yt-dlp`/`FFmpeg` pada `doctor`, `FFmpeg` saat mengunduh, `Deno` saat mengunduh YouTube, atau `gradio` untuk `unduhin ui`).
- `130`: Dihentikan oleh pengguna (`Ctrl+C`).

---

### Pemecahan Masalah

- **`yt-dlp` kedaluwarsa (> 90 hari):** perbarui sesuai instalasi: `uv tool upgrade unduhin`, `pipx runpip unduhin install -U "yt-dlp[default]"`, atau `pip install -U "yt-dlp[default]"`.
- **HTTP 429 (Terlalu banyak permintaan):** backoff bertahap dibatasi maksimal 120 detik per item; 3 item berturut-turut kena 429 akan menghentikan sisa batch (exit `1`).
- **Pemeriksaan anti-bot YouTube (`Sign in to confirm you're not a bot`):** `unduhin` sengaja tidak memakai akun/cookies, sehingga tidak dapat menyelesaikan verifikasi ini. Coba lagi nanti, ganti jaringan, atau perbarui `yt-dlp`.
- **Butuh log detail / traceback:** tambahkan `--verbose`.

---

### Status Pengujian Saat Ini

- **Terverifikasi (Windows / Python 3.13.14):** `198` test unit/keamanan/CLI/pipeline/lazy-import/locale/UI/isolasi-soket lulus (`1` test Unix symlink dilewati di Windows, `2` test live network/integration lulus saat dijalankan dengan flag opt-in `-m "network or integration"`).
- **Terverifikasi di mesin Windows ini:** unduhan live dengan `FFmpeg` 9.0.2 + `Deno` 2.9.8 (MP3 audio 192 kbps, MP4 video H.264+AAC 720p, generic extractor MP3, MP4 audio-only fallback warning, `rename/skip/overwrite`, pembatalan `Ctrl+Break`/`130` tanpa sisa file).
- **Belum terverifikasi di mesin ini:** uji visual & Network DevTools browser nyata (`Playwright` tidak terpasang; lihat `M17`), checklist playlist nyata (`M06`), serta eksekusi CI GitHub Actions lintas OS/Python `3.10–3.12` (`.github/workflows/ci.yml` sudah dibuat, **BELUM dijalankan**). Lihat daftar uji manual di [`docs/MANUAL_TEST.md`](docs/MANUAL_TEST.md).
