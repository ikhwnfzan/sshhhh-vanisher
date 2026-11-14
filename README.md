# Silence Cropper on Render

Files: `Dockerfile`, `app.py`, `requirements.txt`.

1. Put these three files in a GitHub repository (root).
2. On render.com: New + -> Web Service -> connect the repository.
3. Language/Runtime: Docker. Instance type: Free. Deploy.
4. Open the `.onrender.com` address when the deploy finishes.

Limits (set in the Dockerfile, or as Environment Variables in Render's dashboard):

| Name | Value | Meaning |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | 30 | Whole request |
| `MAX_FILE_MB` | 15 | One file |
| `MAX_MINUTES` | 6 | One song |
| `ALLOW_LINK` | 0 | LINK tab off (set 1 and add yt-dlp to use it) |
