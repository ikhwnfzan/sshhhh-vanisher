# Silence Cropper (web)

Trim the quiet intro and outro off MP3 files, right in your browser.
Nothing is uploaded: decoding, trimming and encoding all happen on your own device.

## Features

- Cuts any intro or outro quieter than a level you choose (default **-40 dBFS**)
- Waveform preview: the kept part is blue, the removed part is red
- Keeps title, artist, album, cover art and other ID3 tags
- Single file or batch mode, with **Download all as ZIP**
- Output bitrate: 128, 192, 256 or 320 kbps
- English / Indonesian toggle and dark / light theme (both remembered)
- Limits per file: **25 MB** and **10 minutes** (see below)

## How to use

1. Open the site, choose the **FILE** or **BATCH** tab.
2. Drop MP3 files on the box, or click it to choose files.
3. Adjust **Cut below** (louder level = cuts more) and **Keep padding** if you want a short fade-in room.
4. Press **Trim**, check the preview, then **Download MP3**.

## Deploy on GitHub Pages (free)

1. Create a public repository, for example `silence-cropper`.
2. Upload `index.html` (and this README) to the repository root.
3. Go to **Settings → Pages**, choose **Deploy from a branch**, select `main` and `/ (root)`, then save.
4. After about a minute the site is live at `https://<your-username>.github.io/<repository>/`.

For a custom domain, add it under **Settings → Pages** and point a `CNAME` DNS record to `<your-username>.github.io`.

## Settings you can change

Open `index.html` and edit near the top of the script:

```js
const MAX_MB = 25, MAX_MIN = 10;   // limits per file
```

The hint line and error messages update automatically. Raising the limits is risky on phones,
because a 10-minute stereo song needs a few hundred MB of memory once decoded.

The default threshold is the `value="-40"` on the `#thr` slider.

## How it works

- **Decode:** Web Audio API (`OfflineAudioContext`) at the file's own sample rate.
- **Find cuts:** 10 ms windows, loudness measured as RMS in dBFS; the song is kept from the first to the last window above the threshold.
- **Encode:** [lamejs](https://github.com/zhuker/lamejs) (loaded from cdnjs) writes the new MP3.
- **Tags:** the original ID3v2 tag is copied onto the new file, minus the old length frame (`TLEN`).
- **Limits:** size is checked before reading, length is estimated from the MP3 header before decoding, and checked again after decoding.

## Known limitations

- Needs an internet connection the first time, to load the MP3 encoder. To host it yourself, download `lame.min.js` into the repository and change the `<script>` tag.
- Very long songs or large batches may fail on low-memory devices.
- Re-encoding as MP3 causes a small quality loss, so a high bitrate is the default.
- MP3 input only. ID3v1 tags are not copied.
- There is no link download in the web version. Use the desktop version for that.

## Credits

- [lamejs](https://github.com/zhuker/lamejs) for MP3 encoding (LGPL).

&copy; 2026 ikhwnfzan. Add a `LICENSE` file if you want others to reuse the code.
