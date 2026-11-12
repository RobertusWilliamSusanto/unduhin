"""Bilingual (English and Indonesian) localization utilities."""

from __future__ import annotations

import ctypes
import locale
import os
import sys
from typing import Literal

SupportedLang = Literal["en", "id"]

_INDONESIAN_PREFIXES: tuple[str, ...] = (
    "id",
    "id_id",
    "id-id",
    "in",
    "in_id",
    "in-id",
    "indonesian",
    "bahasa",
)

_ENGLISH_PREFIXES: tuple[str, ...] = (
    "en",
    "english",
)

MESSAGES: dict[SupportedLang, dict[str, str]] = {
    "en": {
        "app.tagline": "Local-first media downloader and converter (MP3 / MP4).",
        "app.legal_title": "Legal Notice",
        "cli.download_help": "Download media from a URL into MP3 (audio) or MP4 (video).",
        "cli.doctor_help": "Check Python, yt-dlp, yt-dlp-ejs, FFmpeg, and Deno availability.",
        "cli.ui_help": "Launch the local web UI bound strictly to 127.0.0.1.",
        "cli.starting_download": "Starting download: {url}",
        "cli.downloading": "Downloading",
        "cli.postprocessing": "Converting / merging media...",
        "cli.retrying": "Transient error; retrying ({attempt}/{max_retries}) in {delay:.1f}s: {reason}",
        "cli.item_succeeded": "[green]OK[/green] {url} -> {path}",
        "cli.item_skipped_exists": "[yellow]SKIPPED (EXISTS)[/yellow] {url}: {reason}",
        "cli.item_skipped_unsupported": "[yellow]SKIPPED (UNSUPPORTED)[/yellow] {url}: {reason}",
        "cli.item_failed": "[red]FAILED[/red] {url}: {reason}",
        "cli.item_planned": "[cyan]DRY-RUN[/cyan] {url} -> {path}",
        "cli.item_batch_renamed_suffix": "(intra-batch rename)",
        "cli.summary_header": "Summary",
        "cli.summary_output_dir": "Output folder: {path}",
        "cli.summary_counts": (
            "Succeeded: {succeeded} | Failed: {failed} | "
            "Skipped (exists): {skipped_exists} | Skipped (unsupported): {skipped_unsupported}"
        ),
        "cli.summary_dry_run": (
            "Planned: {planned} | Failed: {failed} | "
            "Skipped (exists): {skipped_exists} | Skipped (unsupported): {skipped_unsupported}"
        ),
        "cli.summary_all_skipped": "All items were skipped (no files were downloaded).",
        "cli.summary_col_status": "Status",
        "cli.summary_col_target": "Target / URL",
        "cli.summary_col_detail": "Details",
        "cli.duplicates_removed": "Removed {count} duplicate URL(s) from input.",
        "cli.info_title": "Media Metadata",
        "cli.info_field": "Field",
        "cli.info_value": "Value",
        "cli.info_media_title": "Title",
        "cli.info_uploader": "Uploader",
        "cli.info_duration": "Duration",
        "cli.info_live_status": "Live Status",
        "cli.info_est_size": "Estimated Size",
        "cli.info_selected_format": "Selected Format",
        "cli.info_playlist": "Playlist",
        "cli.info_item_count": "Entries",
        "cli.info_unknown": "Unknown",
        "cli.info_yes": "Yes",
        "cli.info_no": "No",
        "cli.info_not_live": "Not live",
        "cli.info_playlist_entries": "Playlist Entries (showing {shown})",
        "cli.interrupted": "Cancelled by user (Ctrl+C). Partial temporary files were cleaned up.",
        "cli.missing_url": "Provide a URL or --batch file to download.",
        "cli.invalid_option": "Invalid option value: {detail}",
        "cli.welcome_title": "unduhin - download media from URLs into MP3 or MP4 locally",
        "cli.welcome_examples": (
            "Examples:\n"
            "  unduhin https://example.com/watch?v=... --mode audio\n"
            "  unduhin https://example.com/watch?v=... --mode video\n"
            "  unduhin ui"
        ),
        "cli.welcome_hint": "Tip: Run `unduhin doctor` to check tools, or `unduhin --help` for all options.",
        "cli.mode_prompt": "Choose output format (1 = MP3 audio, 2 = MP4 video)",
        "cli.mode_prompt_invalid": "Please enter 1 for MP3 or 2 for MP4.",
        "cli.missing_mode": "Specify --mode audio or --mode video (interactive selection requires a terminal).",
        "doctor.title": "unduhin Environment Check",
        "doctor.col_component": "Component",
        "doctor.col_tier": "Tier",
        "doctor.tier_required": "Required",
        "doctor.tier_recommended": "Recommended",
        "doctor.col_status": "Status",
        "doctor.col_details": "Details",
        "doctor.col_fix": "How to fix",
        "doctor.status_ok": "OK",
        "doctor.status_warn": "WARN",
        "doctor.status_missing": "MISSING",
        "doctor.python_ok": "Python {version} (>= 3.10)",
        "doctor.python_old": "Python {version} is below minimum 3.10",
        "doctor.ytdlp_ok": "yt-dlp {version} (installed)",
        "doctor.ytdlp_outdated": "yt-dlp {version} is {days} days old (> 90 days); YouTube extractors may break",
        "doctor.ytdlp_missing": "yt-dlp is not importable in this environment",
        "doctor.ejs_ok": "yt-dlp-ejs {version} (installed)",
        "doctor.ejs_missing": "yt-dlp-ejs is missing; only needed for YouTube JS challenges (install yt-dlp[default])",
        "doctor.ffmpeg_ok": "FFmpeg {version} at {path}",
        "doctor.ffmpeg_ok_not_on_path": "FFmpeg {version} found at {path} (not in PATH; restart terminal or add to PATH)",
        "doctor.ffmpeg_missing": "FFmpeg binary not found in PATH or standard packages",
        "doctor.deno_ok": "Deno {version} at {path}",
        "doctor.deno_ok_not_on_path": "Deno {version} found at {path} (not in PATH; restart terminal or add to PATH)",
        "doctor.deno_old": "Deno {version} at {path} is below yt-dlp minimum 2.3.0 (only needed for YouTube)",
        "doctor.deno_missing": "Deno not found in PATH or standard locations (optional: only required for YouTube)",
        "doctor.summary_ready": "Environment looks good!",
        "doctor.summary_ready_warn": "Core environment is ready; see yellow warnings above (Deno is only required for YouTube).",
        "doctor.summary_issues": "Action needed: fix missing or outdated required components above.",
        "warn.deno_missing": "Deno (>= 2.3.0) was not found. YouTube downloads may fail or miss formats. Run `unduhin doctor` for setup steps.",
        "warn.mp4_audio_only": "Warning: Source media is audio-only; generated MP4 contains no video streams.",
        "warn.mp4_codec_fallback": "Notice: H.264/AAC MP4 streams were unavailable; downloaded MP4 uses fallback codecs ({vcodec}/{acodec}) and may not play on older devices.",
        "warn.stale_tmp_cleanup_failed": "Could not remove stale temporary folder '{path}': {reason}",
        "warn.unknown_filesize_batch": "Estimated file size was unknown for {count} item(s); requiring at least {min_mb} MB free disk space per item.",
        "warn.playlist_truncated": "Playlist truncated to {max_items} item(s) (total reported: {total}).",
        "warn.playlist_depth_exceeded": "Nested playlist exceeded depth limit ({max_depth}) and deeper entries were skipped.",
        "warn.global_item_limit_reached": "Reached global limit of {max_items} items across all inputs; remaining entries were truncated.",
        "status.skipped_exists": "Skipped because destination file already exists: {path}",
        "error.invalid_url": "Invalid URL '{url}': only http:// and https:// URLs with a valid host are allowed.",
        "error.invalid_template": "Invalid filename template '{template}': {reason}",
        "error.output_path_too_long": "Output folder path '{path}' is too long ({length} chars; maximum leaving room for filenames is {max_len} chars). Choose a shorter --output-dir.",
        "error.path_traversal": "Security error: resolved path '{path}' escapes target directory.",
        "error.disallowed_extension": "Blocked unexpected file extension '{ext}' (expected '{expected}').",
        "error.ffmpeg_missing": "FFmpeg is required to convert MP3 or merge MP4 files. Run `unduhin doctor` for installation steps.",
        "error.deno_missing": "Deno (>= 2.3.0) is required by yt-dlp to solve YouTube challenges. Run `unduhin doctor` for installation steps.",
        "error.gradio_missing": "Optional web UI dependency 'gradio' is not installed. Install it with: pip install \"unduhin[ui]\"",
        "error.ui_port_in_use": "Could not start the local UI on 127.0.0.1:{port} because the port is already in use or permission was denied. Choose another port with --port.",
        "error.ui_playlist_rejected": "Playlists are not supported in the web UI. Use the CLI (`unduhin download --playlist`) instead.",
        "error.ui_url_too_long": "URL exceeds the maximum allowed length ({max_len} characters).",
        "error.video_unavailable": "This media is private, deleted, members-only, or unavailable.",
        "error.age_restricted": "This media is age-restricted and requires account sign-in, which unduhin intentionally does not support for privacy and security.",
        "error.bot_check": "Platform requested anti-bot verification (\"Sign in to confirm you're not a bot\"). Because unduhin runs without cookies or account login, it cannot resolve this check. Try again later, switch to another network/IP, or update yt-dlp (`uv tool upgrade unduhin`, `pipx runpip unduhin install -U \"yt-dlp[default]\"`, or `pip install -U \"yt-dlp[default]\"`).",
        "error.geo_blocked": "This media is restricted in your geographic region.",
        "error.live_stream_unsupported": "Live streams and upcoming premieres are not supported.",
        "error.unsupported_url": "This URL is not supported or does not point to downloadable media.",
        "error.format_unavailable": "Requested media format or quality is not available for this URL.",
        "error.drm_protected": "This media is DRM-protected and cannot be downloaded.",
        "error.disk_full": "Insufficient disk space in '{path}'. Free up space and try again.",
        "error.ytdlp_outdated": "Extraction failed (possibly outdated yt-dlp or changed platform format). Update yt-dlp using your installer: `uv tool upgrade unduhin`, `pipx runpip unduhin install -U \"yt-dlp[default]\"`, or `pip install -U \"yt-dlp[default]\"`.",
        "error.http_403_transient": "Access temporarily denied (HTTP 403 Forbidden).",
        "error.network_transient": "Network connection was interrupted or failed: {detail}",
        "error.network_timeout": "Network connection timed out (no response from server, check connection): {detail}",
        "error.rate_limited": "Rate limited by platform (HTTP 429). Please wait before trying again.",
        "error.rate_limit_circuit_breaker": "Stopped remaining items in batch after {count} consecutive rate-limit (HTTP 429) failures. Wait before trying again.",
        "error.playlist_disabled": "URL points to a playlist. Pass --playlist to download playlists.",
        "error.playlist_entry_unavailable": "Playlist entry is unavailable, deleted, or private.",
        "error.batch_file_unreadable": "Batch file '{path}' could not be read: {reason}",
        "error.batch_too_large": "Batch file '{path}' exceeds maximum allowed size ({max_mb} MB).",
        "error.batch_too_many_urls": "Too many URLs in batch (maximum allowed: {max_urls}).",
        "error.batch_invalid_line": "Invalid URL at line {line} in batch file: '{url}'",
        "error.batch_empty": "Batch file '{path}' contains no valid URLs.",
        "error.cancelled": "Download cancelled by user.",
        "error.unexpected": "Download failed due to an unexpected error ({detail}). Re-run with --verbose for diagnostic details.",
        "error.unexpected_cli": "Unexpected error ({detail}). Re-run with --verbose for details.",
        "ui.page_title": "unduhin",
        "ui.app_title": "unduhin",
        "ui.app_subtitle": "Local media converter (MP3 / MP4) running only on 127.0.0.1.",
        "ui.output_dir_label": "Output folder (fixed at launch)",
        "ui.url_label": "Media URL",
        "ui.url_placeholder": "https://example.com/watch?v=...",
        "ui.mode_label": "Format",
        "ui.mode_audio": "MP3 (Audio)",
        "ui.mode_video": "MP4 (Video)",
        "ui.language_label": "Language",
        "ui.theme_label": "Theme",
        "ui.theme_system": "System",
        "ui.theme_light": "Light",
        "ui.theme_dark": "Dark",
        "ui.download_button": "Download",
        "ui.cancel_button": "Cancel",
        "ui.status_label": "Status",
        "ui.status_idle": "IDLE: Ready. Paste one URL and choose MP3 or MP4.",
        "ui.status_validating": "VALIDATING: Checking URL and media metadata...",
        "ui.status_downloading": "DOWNLOADING: Downloading media ({percent})...",
        "ui.status_postprocessing": "CONVERTING: Converting / merging media...",
        "ui.status_retrying": "RETRYING: Temporary error, retry {attempt}/{max_retries}...",
        "ui.status_succeeded": "SUCCEEDED: Saved {filename}",
        "ui.status_skipped_exists": "SKIPPED (EXISTS): File already exists ({filename})",
        "ui.status_skipped_unsupported": "SKIPPED (UNSUPPORTED): {reason}",
        "ui.status_cancelled": "CANCELLED: Download cancelled and temporary files removed.",
        "ui.status_failed": "FAILED: {reason}",
        "ui.summary_label": "Result summary",
        "ui.legal_note": "Use only for content you own or have explicit permission to download.",
    },
    "id": {
        "app.tagline": "Pengunduh dan pengonversi media lokal (MP3 / MP4).",
        "app.legal_title": "Catatan Legal",
        "cli.download_help": "Unduh media dari URL menjadi MP3 (audio) atau MP4 (video).",
        "cli.doctor_help": "Periksa ketersediaan Python, yt-dlp, yt-dlp-ejs, FFmpeg, dan Deno.",
        "cli.ui_help": "Jalankan antarmuka web lokal yang hanya terikat ke 127.0.0.1.",
        "cli.starting_download": "Memulai unduhan: {url}",
        "cli.downloading": "Mengunduh",
        "cli.postprocessing": "Mengonversi / menggabungkan media...",
        "cli.retrying": "Gangguan sementara; mencoba ulang ({attempt}/{max_retries}) dalam {delay:.1f} detik: {reason}",
        "cli.item_succeeded": "[green]BERHASIL[/green] {url} -> {path}",
        "cli.item_skipped_exists": "[yellow]DILEWATI (SUDAH ADA)[/yellow] {url}: {reason}",
        "cli.item_skipped_unsupported": "[yellow]DILEWATI (TIDAK DIDUKUNG)[/yellow] {url}: {reason}",
        "cli.item_failed": "[red]GAGAL[/red] {url}: {reason}",
        "cli.item_planned": "[cyan]DRY-RUN[/cyan] {url} -> {path}",
        "cli.item_batch_renamed_suffix": "[ganti nama intra-batch]",
        "cli.summary_header": "Ringkasan",
        "cli.summary_output_dir": "Folder output: {path}",
        "cli.summary_counts": (
            "Berhasil: {succeeded} | Gagal: {failed} | "
            "Dilewati (sudah ada): {skipped_exists} | Dilewati (tidak didukung): {skipped_unsupported}"
        ),
        "cli.summary_dry_run": (
            "Rencana: {planned} | Gagal: {failed} | "
            "Dilewati (sudah ada): {skipped_exists} | Dilewati (tidak didukung): {skipped_unsupported}"
        ),
        "cli.summary_all_skipped": "Semua item dilewati (tidak ada file yang diunduh).",
        "cli.summary_col_status": "Status",
        "cli.summary_col_target": "Target / URL",
        "cli.summary_col_detail": "Detail",
        "cli.duplicates_removed": "Membuang {count} URL duplikat dari input.",
        "cli.info_title": "Metadata Media",
        "cli.info_field": "Bidang",
        "cli.info_value": "Nilai",
        "cli.info_media_title": "Judul",
        "cli.info_uploader": "Pengunggah",
        "cli.info_duration": "Durasi",
        "cli.info_live_status": "Status Live",
        "cli.info_est_size": "Perkiraan Ukuran",
        "cli.info_selected_format": "Format Terpilih",
        "cli.info_playlist": "Playlist",
        "cli.info_item_count": "Jumlah Item",
        "cli.info_unknown": "Tidak diketahui",
        "cli.info_yes": "Ya",
        "cli.info_no": "Tidak",
        "cli.info_not_live": "Bukan live",
        "cli.info_playlist_entries": "Daftar Item Playlist (menampilkan {shown})",
        "cli.interrupted": "Dibatalkan oleh pengguna (Ctrl+C). File sementara sudah dibersihkan.",
        "cli.missing_url": "Masukkan URL atau file --batch untuk diunduh.",
        "cli.invalid_option": "Nilai opsi tidak valid: {detail}",
        "cli.welcome_title": "unduhin - unduh media dari URL menjadi MP3 atau MP4 secara lokal",
        "cli.welcome_examples": (
            "Contoh:\n"
            "  unduhin https://example.com/watch?v=... --mode audio\n"
            "  unduhin https://example.com/watch?v=... --mode video\n"
            "  unduhin ui"
        ),
        "cli.welcome_hint": "Tips: Jalankan `unduhin doctor` untuk cek perangkat, atau `unduhin --help` untuk semua opsi.",
        "cli.mode_prompt": "Pilih format output (1 = audio MP3, 2 = video MP4)",
        "cli.mode_prompt_invalid": "Masukkan 1 untuk MP3 atau 2 untuk MP4.",
        "cli.missing_mode": "Tentukan --mode audio atau --mode video (pemilihan interaktif membutuhkan terminal).",
        "doctor.title": "Pemeriksaan Lingkungan unduhin",
        "doctor.col_component": "Komponen",
        "doctor.col_tier": "Tingkat",
        "doctor.tier_required": "Wajib",
        "doctor.tier_recommended": "Disarankan",
        "doctor.col_status": "Status",
        "doctor.col_details": "Detail",
        "doctor.col_fix": "Cara memperbaiki",
        "doctor.status_ok": "OK",
        "doctor.status_warn": "PERINGATAN",
        "doctor.status_missing": "TIDAK ADA",
        "doctor.python_ok": "Python {version} (>= 3.10)",
        "doctor.python_old": "Python {version} di bawah batas minimum 3.10",
        "doctor.ytdlp_ok": "yt-dlp {version} (terpasang)",
        "doctor.ytdlp_outdated": "yt-dlp {version} sudah berumur {days} hari (> 90 hari); ekstraktor YouTube berisiko gagal",
        "doctor.ytdlp_missing": "yt-dlp tidak ditemukan di lingkungan Python ini",
        "doctor.ejs_ok": "yt-dlp-ejs {version} (terpasang)",
        "doctor.ejs_missing": "yt-dlp-ejs tidak ada; hanya dibutuhkan untuk tantangan JS YouTube (pasang yt-dlp[default])",
        "doctor.ffmpeg_ok": "FFmpeg {version} di {path}",
        "doctor.ffmpeg_ok_not_on_path": "FFmpeg {version} ditemukan di {path} (belum ada di PATH; restart terminal atau tambahkan ke PATH)",
        "doctor.ffmpeg_missing": "Biner FFmpeg tidak ditemukan di PATH maupun paket standar",
        "doctor.deno_ok": "Deno {version} di {path}",
        "doctor.deno_ok_not_on_path": "Deno {version} ditemukan di {path} (belum ada di PATH; restart terminal atau tambahkan ke PATH)",
        "doctor.deno_old": "Deno {version} di {path} di bawah batas minimum yt-dlp 2.3.0 (hanya dibutuhkan untuk YouTube)",
        "doctor.deno_missing": "Deno tidak ditemukan di PATH maupun lokasi standar (opsional: hanya dibutuhkan untuk YouTube)",
        "doctor.summary_ready": "Lingkungan siap digunakan!",
        "doctor.summary_ready_warn": "Lingkungan inti siap digunakan; periksa peringatan kuning di atas (Deno hanya dibutuhkan untuk YouTube).",
        "doctor.summary_issues": "Perlu tindakan: perbaiki komponen wajib yang hilang atau usang di atas.",
        "warn.deno_missing": "Deno (>= 2.3.0) tidak ditemukan. Unduhan YouTube dapat gagal atau kehilangan format. Jalankan `unduhin doctor` untuk panduan instalasi.",
        "warn.mp4_audio_only": "Peringatan: Sumber media hanya audio; file MP4 yang dihasilkan tidak memuat video (audio-only).",
        "warn.mp4_codec_fallback": "Peringatan: Stream MP4 H.264/AAC tidak tersedia; file MP4 memakai codec cadangan ({vcodec}/{acodec}) dan mungkin tidak berjalan di pemutar lama.",
        "warn.stale_tmp_cleanup_failed": "Gagal menghapus folder sementara basi '{path}': {reason}",
        "warn.unknown_filesize_batch": "Perkiraan ukuran file tidak diketahui untuk {count} item; mensyaratkan ruang disk bebas minimal {min_mb} MB per item.",
        "warn.playlist_truncated": "Playlist dipotong menjadi {max_items} item (total tercatat: {total}).",
        "warn.playlist_depth_exceeded": "Playlist bersarang melebihi batas kedalaman ({max_depth}); item yang lebih dalam dilewati.",
        "warn.global_item_limit_reached": "Mencapai batas global {max_items} item lintas semua input; entri yang tersisa dipotong.",
        "status.skipped_exists": "Dilewati karena file tujuan sudah ada: {path}",
        "error.invalid_url": "URL tidak valid '{url}': hanya skema http:// dan https:// dengan host valid yang diizinkan.",
        "error.invalid_template": "Template nama file tidak valid '{template}': {reason}",
        "error.output_path_too_long": "Path folder output '{path}' terlalu panjang ({length} karakter; batas agar cukup untuk nama file adalah {max_len} karakter). Pilih --output-dir yang lebih pendek.",
        "error.path_traversal": "Error keamanan: path tujuan '{path}' keluar dari folder output.",
        "error.disallowed_extension": "Ekstensi file '{ext}' ditolak (seharusnya '{expected}').",
        "error.ffmpeg_missing": "FFmpeg wajib tersedia untuk konversi MP3 atau penggabungan MP4. Jalankan `unduhin doctor` untuk panduan instalasi.",
        "error.deno_missing": "Deno (>= 2.3.0) dibutuhkan yt-dlp untuk menyelesaikan tantangan YouTube. Jalankan `unduhin doctor` untuk panduan instalasi.",
        "error.gradio_missing": "Dependensi UI web opsional 'gradio' belum terpasang. Pasang dengan perintah: pip install \"unduhin[ui]\"",
        "error.ui_port_in_use": "Tidak dapat menjalankan UI lokal di 127.0.0.1:{port} karena port sudah terpakai atau izin ditolak. Pilih port lain dengan --port.",
        "error.ui_playlist_rejected": "Playlist tidak didukung di UI web. Gunakan CLI (`unduhin download --playlist`).",
        "error.ui_url_too_long": "URL melebihi batas panjang maksimum ({max_len} karakter).",
        "error.video_unavailable": "Media ini bersifat privat, telah dihapus, khusus pelanggan berbayar (members-only), atau tidak tersedia.",
        "error.age_restricted": "Media ini dibatasi usia dan memerlukan login akun, yang sengaja tidak didukung unduhin demi privasi dan keamanan.",
        "error.bot_check": "Platform meminta verifikasi anti-bot (\"Sign in to confirm you're not a bot\"). Tanpa cookies atau login akun, masalah ini tidak dapat diselesaikan oleh unduhin. Coba lagi nanti, ganti jaringan/IP, atau perbarui yt-dlp (`uv tool upgrade unduhin`, `pipx runpip unduhin install -U \"yt-dlp[default]\"`, atau `pip install -U \"yt-dlp[default]\"`).",
        "error.geo_blocked": "Media ini dibatasi di wilayah geografis Anda.",
        "error.live_stream_unsupported": "Siaran langsung (live stream) dan tayangan perdana mendatang tidak didukung.",
        "error.unsupported_url": "URL ini tidak didukung atau tidak mengarah ke media yang bisa diunduh.",
        "error.format_unavailable": "Format atau kualitas media yang diminta tidak tersedia untuk URL ini.",
        "error.drm_protected": "Media ini dilindungi DRM dan tidak dapat diunduh.",
        "error.disk_full": "Ruang disk di '{path}' tidak mencukupi. Kosongkan ruang lalu coba lagi.",
        "error.ytdlp_outdated": "Ekstraksi gagal (kemungkinan yt-dlp usang atau format platform berubah). Perbarui sesuai cara instalasi Anda: `uv tool upgrade unduhin`, `pipx runpip unduhin install -U \"yt-dlp[default]\"`, atau `pip install -U \"yt-dlp[default]\"`.",
        "error.http_403_transient": "Akses ditolak sementara oleh platform (HTTP 403 Forbidden).",
        "error.network_transient": "Koneksi jaringan terputus atau gagal: {detail}",
        "error.network_timeout": "Koneksi jaringan kehabisan waktu (tidak ada respons dari server, periksa koneksi): {detail}",
        "error.rate_limited": "Permintaan dibatasi oleh platform (HTTP 429). Tunggu beberapa saat lalu coba lagi.",
        "error.rate_limit_circuit_breaker": "Sisa item dalam batch dihentikan setelah {count} kegagalan batas laju (HTTP 429) berturut-turut. Tunggu sebelum mencoba lagi.",
        "error.playlist_disabled": "URL mengarah ke playlist. Tambahkan opsi --playlist untuk mengunduh playlist.",
        "error.playlist_entry_unavailable": "Item playlist tidak tersedia, telah dihapus, atau bersifat privat.",
        "error.batch_file_unreadable": "File batch '{path}' tidak dapat dibaca: {reason}",
        "error.batch_too_large": "Ukuran file batch '{path}' melebihi batas maksimum ({max_mb} MB).",
        "error.batch_too_many_urls": "Terlalu banyak URL dalam batch (batas maksimum: {max_urls}).",
        "error.batch_invalid_line": "URL tidak valid pada baris {line} di file batch: '{url}'",
        "error.batch_empty": "File batch '{path}' tidak berisi URL yang valid.",
        "error.cancelled": "Unduhan dibatalkan oleh pengguna.",
        "error.unexpected": "Unduhan gagal karena galat tak terduga ({detail}). Jalankan ulang dengan --verbose untuk melihat detail diagnostik.",
        "error.unexpected_cli": "Terjadi galat tak terduga ({detail}). Jalankan ulang dengan --verbose untuk detailnya.",
        "ui.page_title": "unduhin",
        "ui.app_title": "unduhin",
        "ui.app_subtitle": "Pengonversi media lokal (MP3 / MP4) yang hanya berjalan di 127.0.0.1.",
        "ui.output_dir_label": "Folder output (ditetapkan saat peluncuran)",
        "ui.url_label": "URL Media",
        "ui.url_placeholder": "https://example.com/watch?v=...",
        "ui.mode_label": "Format",
        "ui.mode_audio": "MP3 (Audio)",
        "ui.mode_video": "MP4 (Video)",
        "ui.language_label": "Bahasa",
        "ui.theme_label": "Tema",
        "ui.theme_system": "Sistem",
        "ui.theme_light": "Terang",
        "ui.theme_dark": "Gelap",
        "ui.download_button": "Unduh",
        "ui.cancel_button": "Batal",
        "ui.status_label": "Status",
        "ui.status_idle": "SIAP: Tempel satu URL lalu pilih MP3 atau MP4.",
        "ui.status_validating": "MEMVALIDASI: Memeriksa URL dan metadata media...",
        "ui.status_downloading": "MENGUNDUH: Mengunduh media ({percent})...",
        "ui.status_postprocessing": "MENGONVERSI: Mengonversi / menggabungkan media...",
        "ui.status_retrying": "MENCOBA ULANG: Gangguan sementara, percobaan {attempt}/{max_retries}...",
        "ui.status_succeeded": "BERHASIL: Tersimpan sebagai {filename}",
        "ui.status_skipped_exists": "DILEWATI (SUDAH ADA): File sudah ada ({filename})",
        "ui.status_skipped_unsupported": "DILEWATI (TIDAK DIDUKUNG): {reason}",
        "ui.status_cancelled": "DIBATALKAN: Unduhan dibatalkan dan file sementara telah dihapus.",
        "ui.status_failed": "GAGAL: {reason}",
        "ui.summary_label": "Ringkasan hasil",
        "ui.legal_note": "Gunakan hanya untuk konten milik Anda atau konten yang Anda miliki izinnya.",
    },
}


def normalize_locale(raw_locale: str | None) -> SupportedLang | None:
    """Map a raw locale string (POSIX or Windows format) to ``'en'`` or ``'id'``."""
    if not raw_locale:
        return None
    cleaned = raw_locale.strip().lower()
    if not cleaned or cleaned in {"c", "posix"}:
        return None

    # Strip encoding and modifiers such as ".utf-8", ".1252", "@euro"
    base = cleaned.split(".", 1)[0].split("@", 1)[0].strip()
    if not base:
        return None

    for prefix in _INDONESIAN_PREFIXES:
        if (
            base == prefix
            or base.startswith(f"{prefix}_")
            or base.startswith(f"{prefix}-")
            or base.startswith(f"{prefix} ")
        ):
            return "id"

    for prefix in _ENGLISH_PREFIXES:
        if (
            base == prefix
            or base.startswith(f"{prefix}_")
            or base.startswith(f"{prefix}-")
            or base.startswith(f"{prefix} ")
        ):
            return "en"

    return None


def _detect_windows_ui_locale() -> SupportedLang | None:
    """Detect user UI language on Windows via Kernel32 LCID when available."""
    if sys.platform != "win32":
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    try:
        lcid = int(windll.kernel32.GetUserDefaultUILanguage())
    except (AttributeError, OSError, ValueError):
        return None

    # Primary language ID 0x21 corresponds to Indonesian (LANG_INDONESIAN)
    if (lcid & 0x3FF) == 0x21:
        return "id"
    mapped = locale.windows_locale.get(lcid)
    return normalize_locale(mapped)


def detect_language(explicit_lang: str | None = None) -> SupportedLang:
    """Resolve user-facing language (``'id'`` or ``'en'``), defaulting to English."""
    if explicit_lang:
        explicit = normalize_locale(explicit_lang)
        if explicit is not None:
            return explicit

    for env_var in ("UNDUHIN_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        env_val = os.environ.get(env_var)
        if env_val:
            first_part = env_val.split(":", 1)[0]
            resolved = normalize_locale(first_part)
            if resolved is not None:
                return resolved

    try:
        loc_name, _ = locale.getlocale()
    except ValueError:
        loc_name = None

    resolved = normalize_locale(loc_name)
    if resolved is not None:
        return resolved

    win_lang = _detect_windows_ui_locale()
    if win_lang is not None:
        return win_lang

    return "en"


def t(key: str, *, lang: str | None = None, **kwargs: object) -> str:
    """Translate ``key`` into the resolved language with safe parameter formatting."""
    resolved_lang = detect_language(lang)
    catalog = MESSAGES.get(resolved_lang, MESSAGES["en"])
    template = catalog.get(key) or MESSAGES["en"].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        return template


def safe_console_text(text: str, *, encoding: str | None = None) -> str:
    """Replace characters unrepresentable by ``encoding`` (default stdout encoding) safely."""
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(target_encoding, errors="strict")
        return text
    except UnicodeEncodeError:
        return text.encode(target_encoding, errors="replace").decode(
            target_encoding, errors="replace"
        )
    except LookupError:
        return text.encode("ascii", errors="replace").decode("ascii", errors="replace")
