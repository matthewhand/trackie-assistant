# Regenerate docs/demo.gif.
#
# Requires vhs (https://github.com/charmbracelet/vhs), which in turn needs
# ttyd and ffmpeg. Records a real run against a public profile, so it needs
# network access and `playwright install chromium`.
#
#   vhs scripts/demo.tape
#
Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1000
Set Height 420
Set Padding 18
Set Theme "Dracula"
Set TypingSpeed 25ms
Set CursorBlink false

Type "suno-dl --url https://suno.com/@drinko --limit 3 --dry-run"
Sleep 400ms
Enter
Sleep 4s

Type "suno-dl --url https://suno.com/@drinko --limit 3 --output-dir ./downloads"
Sleep 400ms
Enter
Sleep 6s

Type "ls -la downloads/"
Sleep 300ms
Enter
Sleep 2s
