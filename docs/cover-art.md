# Cover images and Music Assistant

The CLI, Python download entry point, dashboard and HA add-on default to cover
images on and retained videos off. --no-covers / SUNO_COVERS=0 opts out of cover
downloads; --videos is a separate opt-in. The Linux additions engine may use an explicitly published MP4 as a temporary
audio or cover source when extraction is enabled; no MP4 is retained.

For each new recording, the Linux additions engine fetches the image URL from
the verified clip page, accepts bounded decodable JPEG/PNG image content, saves
a <track>.cover.jpg or <track>.cover.png, and embeds the exact same image
as an iTunes covr atom in the staged M4A. The audio packet signature must remain
unchanged. Audio, image and metadata publish together through the existing
recoverable group protocol. Invalid or unavailable artwork is skipped with a
warning; it cannot publish a video as a cover. HTTP 429 stops the run.

Music Assistant's filesystem provider reads embedded artwork from the M4A.
The media share can remain read-only. Its normal library scan discovers newly
published tracks and artwork; no separate image share, public Suno URL or extra
copy of the audio is needed. Existing <track>.cover.jpg sidecars alone are not
a guarantee of per-track artwork detection.

Existing indexed recordings remain unchanged, including when they lack covers.
The older operational suno-verified-sync command is a separate two-file
publication path and does not implement cover fetching. Do not infer artwork
backfill from the previous CT107 run. Existing-library artwork repair requires
a separately prepared offline update; this change does not run it.

Development and acceptance run on the designated Linux development host.
This source change has not been deployed to CT107 or HAOS.
The local candidate is 1.6.2.dev20261106+published.2 (HA add-on 1.6.2-dev.4).
