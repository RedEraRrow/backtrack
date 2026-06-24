# TODO

Legend: ✅ done · 🟡 partial · ⬜ open

✅ 1.  implement addition nav options with b/→/← etc.  (select: ←/b/h/Esc = back, →/l = confirm, ↑↓/jk = move, q = quit)
✅ 2.  make lead-in time ui cleaner  (prefilled with current value + validated, clamps ≥0)
✅ 3.  tidy up clear history log  (shows entry count, no-ops when empty, single-line status)
⬜ 4.  add margins globally (except album art in standard and narrow views, and horizontal rules) to accomoate for readability for those with 0 terminal margins
✅ 5.  when a setting is changed, the ui should stay on the current item rather than going to the top  (select now takes index=; settings menu remembers it)
✅ 6.  add clear library xml path if not wanted  (Settings → "Clear iTunes Library XML Path", only shown when a path is set)
✅ 7.  section settings  (PLAYBACK / LIBRARY / EDITORS / HISTORY headings via new prompt.separator())
⬜ 8.  improve search results to make more obvious the findings by showing what was matched in a pretty way
✅ 9.  in artist and genre browse, it should show an album list within each artist even if only one album (albums are different)  (no more single-album auto-skip)
✅ 10. quit should be global exit to terminal  (lowercase q = save-current-state-and-quit: navigation menus quit immediately; value editors commit the in-progress edit first, then quit on the next menu. q stays literal only in free-text fields)
✅ 11. back should be universal back button  (←/b/h/Esc returns from any select()/checkbox menu, incl. editors)
✅ 12. if only one track in an album or group, don't display play all button or bulk edit button  (track + album level)
✅ 13. add queue list view to playback  (single 'w' key cycles the right column: off → lyrics → queue → lyrics+credits; unavailable views skipped)
⬜ 14. allow playback pop out into another window, so you can still browse/edit
✅ 15. ensure bulk editor uses specific prompts  (bulk "Add New Tag" now reuses prompt_for_value like single-track editing)
🟡 16. audit all tags in registry to ensure the are using the proper input method  (dedicated interactive graphic-EQ widget for EQU2 — ISO bands + custom, presets, response curve; volume editor for RVA2; broader numeric/rating audit still open)
✅ 17. ensure bulk edit boxes at the top span the whole width  (bulk header box now spans full terminal width)
⬜ 18. POST-DEV: ensure default options are for a more general userbase, not for me testing
✅ 19. generalise language for audio rather than just radio content  (player credits: CAST→PERFORMERS / PRODUCTION TEAM; 'c' hint = credits)
⬜ 20. ensure album and artist always displays, then metadata toggle is only for year, disc, movement, track, etc.
✅ 21. visual volume indicator  (full-height vertical bar to the right of album art, incl. the credits/lyrics split view; live +/- updates)
✅ 22. ensure all hints are logical  (consistent back/quit wording across all widgets; 'space' not 'spc'; right column is one 'panel' hint)
🟡 23. consider a more tui shortcut system similar to vim etc.  (hjkl navigation + select() shortcuts= param; fuller scheme still open)
✅ 24. lock mouse clicks to only activate when over text or button, not empty space in the same row  (select() + checkbox())
✅ 25. make track items have more data if e.g. different track to album artists, duration etc.  (track rows show featured-artist marker + duration; duration now cached)
✅ 26. filenames may be esoteric so use track data instead for titles etc.  (metadata editor shows an explicit, clearly-labelled "File path" row marked as not ID3)
✅ 27. remove add tag button, make a shortcut instead  (Add Tag is now the 'a' shortcut in the metadata editor)
✅ 28. absolute library paths (how is this not already in there???)  (build_library + config now resolve via abspath/expanduser)
⬜ 29. possibly add writing credits to the end of lyrics
⬜ 30. fix boundary window size duplication
⬜ 31. if nothing else in queue, and no lyrics, and no credits, w key should do nothing in playback
⬜ 32. ensure that no matter wat window size, no matter how small or large, all content looks good
⬜ 33. make the additional metadata when browsing look a bit nicer (columns, dimming etc.) and add album artist to album in browse
⬜ 34. disc/work seperators should be made a bit cleaner and prettier
⬜ 35. fix lyric editor to make work with mouse controls, and also make buffer properly, as currently it duplicates with each frame
⬜ 36. make filepath choice in metadata editor have white when inactive, and white and bold when active. have dimmed hint that is just (filesystem)
⬜ 37. bold friendly name when item is active, do not bold tag type
⬜ 38. make import list from text give a template, but also allow to import from existing file
⬜ 39. in the in-place field editors, when widndow deselected, make the cursor a hollow sqaure instead of filled, similar to normal text cursor
⬜ 40. when doing system edit, ensure existing text is being fed in properly, and add setting to choose editor (vim, nvim, nano, emacs, etc., only make options available that are installed already)
⬜ 41. pressing b in playback should return to the previous page, being either previous track, current track browse page, or wherever you were before
⬜ 42. if adding a new sort order, it should set the default text to the original tag, with changes to do ", The" at the end instead of the start etc. (it should guess the sort order based off the matching tag)
⬜ 43. re-add the setting to choose preferred friendly names for tags (with type-hinting from the other options in the tag registry)
⬜ 44. listening history should show the nice track info, not filename, and should also show time listened in a neater format
⬜ 45. while developing, consistently do pylance checks to ensure it is happy
⬜ 46. add better error handling for non-mp3 files
⬜ 47. remove edit lyrics button for anything without a trace of lyrics (no uslt, sylt, md, or json)
⬜ 48. update readmes to be comprehensive, add another readme for filesystem advice to maximise automatic detection of files, and another for tag etiquette
⬜ 49. hints that are disabled should not be shown as an option i.e. if there is nothing else in the queue, no lyrics or credits, then the w key should not show as a hint
⬜ 50. volume on 10% and lower is silent for some reason, it should be somewhat audible except at 0%
⬜ 51. there are still issues with the metadata show/hiding show/hiding the album and artist, and never showing the rest of the metadata at all in some tracks
⬜ 52. b/← should globally be the back button except when in a text field, then it should be esc, and the same with →/↵ for select/next
⬜ 53. when a tag is as such: "APIC:Front Cover", the friendly name is white instead of dimmed, but honestly, for these sorts of table prompts, it should be passed in such that it doesn't require reparsing where there could be possibility of error
⬜ 54. friendly names still aren't filling the column then truncating, they are truncating too early, this is for the single track edit
⬜ 55. generally, when returning to a list with the back button, it should place the cursor on the item it was on before
⬜ 56. TDRC and other ISO8601 should autodetect all possible formats to correct from including YYYY, YYYY-MM, YYYY-MM-DD, YYYY-MM-DD HH, etc. also including other delimeters like "/", "|", "," etc. and allowing for with or without the T, and also adding timezone support in the visual selector as well optionally (should be a dot-matrix world map with dividing lines for timezones, make it selectable (also allow friendly names like GMT, BST, CST, PT, Pacific Time, British Summer Time, etc.)) if anything isn't 100% clear on the format, ask the user
⬜ 57. the queue visual should be a bit more balanced and central, it looks a bit off at the moment
⬜ 58. when doing mouse select, it should move the cursor to there, and then upon second click it should actually select. if not over the words themselves, it should. never actually select.
⬜ 59. add more eq presets, make it a pretty list that is in-place rather than going to a new screen, use all the standard ones including genre presets, all of it
⬜ 60. quite a lot of plain text tags support having multiple values, which is useful and can be utilised in picard, but it would be helpful to have that as an option in here as well
⬜ 61. add copy tag across tracks if one has the right tags and the user forgot to do it in bulk and wants to save time
⬜ 62. generally steer towards power users and weirdos where possible, add more settings for default behaviour, allow plain text edit in most cases as an extra option, which could be set as default if the user wishes
⬜ 63. make default friendly names (the order of lists of friendly names in tag registry) as logical as possible to cater for the favoured friendly name. this involved also adding more friendly names to each tag as well.
⬜ 64. VERY MUCH NOT URGENT: theming, choosing accent colour, background colour etc, option to set playback background to be a colour from the album art
⬜ 65. URGENT: add anything to the list that I have already discussed and/or fixed with claude, with correct markings, ongoing
⬜ 66. remove the need for the lyric_timer.py file, redundant and old, unneeded, check anythimg that is dependent on it and move the needed functionality to a sensible file
⬜ 67. for the mds, also include an md on formatting for lyrics files etc.
⬜ 68. NOTE TO SELF: think about checking xml and m4p logic, in my recent memory it didn't work so i ignored it
⬜ 69. option to import to sylt from json and uslt or json and md, as well as option to import to uslt from txt, rtf, md etc.
⬜ 70. ONGOING: ensure up-to-date mds, docstrings, and all other text. ensure all functions, classes, files etc. across the project have docstrings.
⬜ 71. i tried to get viu to work with proper images but it messed up the spacing and layout massively, so if you can get it to work cleanly go for it, but otherwise remove viu as a dependency and just calculate the half blocks in-project instead
⬜ 72. ensure all dependencies are listed in requirements.txt and pyproject.toml
⬜ 73. NOT URGENT: ensure no segmentation fault on mobile terminal (investigate), and general cross-compatibility

## IDEA 
How about with bulk editing, being able to set a pattern
i.e. with original release dates, you could set them periodic if it's radio episodes
or if the first 8 tracks in an album were released at one time, and the last two are released another, you should be able to choose the ranges and set independently
e.g. once a week from a starting date or maybe something more complicated, maybe series 1 broadcast at 18:30, but the other series broadcast at 11:30
you should be able to set all sorts of complex patterns.
and then you should be able to set disc subtitles or work names to be periodic or for a certain range
e.g. every 6 episodes is a new series, so first 6: TSST = Series 1, second 6: TSST = Series 2
or the first 6 are TIT1 = The Marriage of Figaro Act I, the next 7 are TIT1 = The Marriage of Figaro Act II
and also movement systems prefer track numbers to be relative to the whole album, while disc systems prefer the track numbers to be relative to the disc, i should be able to bulk edit to switch between these
so maybe a movement/disc converter
regex support as well
when adding sort orders in bulk, you should be able to just use the default parsed sort order on each one to save time, or choose a default preferred pattern, 
e.g. i like if there are multiple artists if the artist is say:
"Jeff Goldblum & The Mildred Snitzer Orchestra Feat. dodie" 
the sort order by default could be:
"Goldblum, Jeff/Mildred Snitzer Orchestra, The/dodie"
but this could be intelligent with user verification, so it can match all of the common patterns, and the user can choose the output pattern
and if setting album art, if files all have the same pattern of track file relativity to the cover with similar file naming conventions, you can automatically apply the file-specific cover
this does vaguely also require the tag and filesystem etiquette md todo above

Notes / needs a live TUI pass:
- #13 queue/lyrics/credits cycle ('w') renders in wide + standard layouts; in minimal (<60 cols) the right column isn't drawn (no room), so cycling there only hides lyrics. Worth a look on a real terminal.
- #21 volume bar now also draws in the split view (sits in the gutter between art and the pane); only suppressed in minimal layouts with no horizontal room. Check the gutter spacing looks right.
- #16 EQU2/RVA2 frame creation is verified by round-trip; the new graphic-EQ widget (prompt.equaliser_edit) renders correctly headlessly but wants a live interactive check (mouse mapping, curve overlay, presets).
- #10 q-in-editors uses a deferred-quit flag: the editor commits, the caller saves, then the next menu unwinds. Confirm the save lands before exit on a real run.