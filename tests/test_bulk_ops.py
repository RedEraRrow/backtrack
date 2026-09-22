"""Headless tests for src/id3/bulk_ops.py — the plan/apply core the bulk menu
and the CLI both drive. Fixtures are tag-only MP3s: these operations read and
write frames, never audio."""
import os
import shutil
import tempfile
import unittest

from mutagen.id3 import ID3, APIC, TRCK, TPOS, TLEN, TDLY  # type: ignore[reportPrivateImportUsage]

from src.id3 import bulk_ops as bo


def _mp3(path: str, *, track: str | None = None, disc: str | None = None,
         tlen: int | None = None, tdly: int | None = None,
         pic_types: list | None = None) -> str:
    """A tag-only MP3 carrying exactly the frames a test needs."""
    open(path, "wb").close()
    audio = ID3()
    if track is not None:
        audio.add(TRCK(encoding=3, text=[track]))
    if disc is not None:
        audio.add(TPOS(encoding=3, text=[disc]))
    if tlen is not None:
        audio.add(TLEN(encoding=3, text=[str(tlen)]))
    if tdly is not None:
        audio.add(TDLY(encoding=3, text=[str(tdly)]))
    for i, pt in enumerate(pic_types or []):
        audio.add(APIC(encoding=3, mime="image/jpeg", type=pt,
                       desc=f"c{i}", data=b"\xff\xd8\xff\xd9"))
    audio.save(path, v2_version=3)
    return path


class _Fixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _album(self, spec: list) -> list:
        """One MP3 per (track, disc) pair, named so the order is unambiguous."""
        return [_mp3(os.path.join(self.tmp, f"{i:02d}.mp3"), track=t, disc=d)
                for i, (t, d) in enumerate(spec)]


class RenumberTest(_Fixtures):
    def setUp(self):
        super().setUp()
        # Two discs of two, numbered per-disc.
        self.paths = self._album([("1/2", "1/2"), ("2/2", "1/2"),
                                  ("1/2", "2/2"), ("2/2", "2/2")])
        self.ordered, self.skipped = bo.read_numbering(self.paths)

    def test_read_numbering_orders_by_disc_then_track(self):
        self.assertEqual([s['disc'] for s in self.ordered], ['1', '1', '2', '2'])
        self.assertEqual([s['track'] for s in self.ordered], ['1', '2', '1', '2'])
        self.assertEqual(self.skipped, 0)

    def test_continuous_numbers_across_discs(self):
        plan = bo.plan_renumber(self.ordered, 0, 'continuous')
        self.assertEqual([c.fields['track'] for c in plan.changes], [1, 2, 3, 4])
        self.assertTrue(all(c.fields['total_tracks'] == 4 for c in plan.changes))

    def test_per_disc_restarts_at_one(self):
        plan = bo.plan_renumber(self.ordered, 0, 'per_disc')
        self.assertEqual([c.fields['track'] for c in plan.changes], [1, 2, 1, 2])

    def test_why_reads_as_a_transition(self):
        plan = bo.plan_renumber(self.ordered, 0, 'continuous')
        self.assertEqual(plan.changes[2].why, "1 → 3/4")

    def test_no_writable_files_carries_a_message(self):
        plan = bo.plan_renumber([], 3, 'continuous')
        self.assertEqual(plan.message, "No MP3/MP4 tracks to renumber.")
        self.assertFalse(plan)

    def test_unsupported_formats_are_counted_not_planned(self):
        odd = os.path.join(self.tmp, "cover.txt")
        open(odd, "w").close()
        _ordered, skipped = bo.read_numbering(self.paths + [odd])
        self.assertEqual(skipped, 1)


class ReflowTest(_Fixtures):
    def test_a_parked_disc_shifts_everything_above_it(self):
        paths = self._album([("1", "1"), ("1", "1.5"), ("1", "2")])
        ordered, skipped = bo.read_numbering(paths)
        plan = bo.plan_reflow(ordered, skipped)
        self.assertEqual([c.fields['disc'] for c in plan.changes], [1, 2, 3])
        self.assertIn("disc 1.5 → 2/3", plan.changes[1].why)

    def test_a_deleted_disc_closes_its_gap(self):
        paths = self._album([("1", "1"), ("1", "3")])
        ordered, skipped = bo.read_numbering(paths)
        plan = bo.plan_reflow(ordered, skipped)
        self.assertEqual([c.fields['disc'] for c in plan.changes], [1, 2])

    def test_already_dense_says_so_instead_of_previewing(self):
        paths = self._album([("1", "1/2"), ("1", "2/2")])
        ordered, skipped = bo.read_numbering(paths)
        plan = bo.plan_reflow(ordered, skipped)
        self.assertIsNotNone(plan.message)
        self.assertIn("already 1…2", plan.message or "")

    def test_unchanged_rows_stay_listed_but_unticked(self):
        paths = self._album([("1", "1"), ("1", "3")])
        ordered, skipped = bo.read_numbering(paths)
        plan = bo.plan_reflow(ordered, skipped)
        # Disc 1 keeps its number; only its total moves, so it is still a change.
        self.assertEqual(len(plan.changes), 2)


class StripSingleDiscTest(_Fixtures):
    def _plan(self, spec):
        ordered, skipped = bo.read_numbering(self._album(spec))
        return bo.plan_strip_single_disc(ordered, skipped)

    def test_one_of_one_is_removed(self):
        plan = self._plan([("1", "1/1"), ("2", "1/1")])
        self.assertEqual(len(plan.changed), 2)
        self.assertEqual(plan.changes[0].why, "disc 1/1 → —")

    def test_bare_one_is_removed_when_nothing_sits_on_another_disc(self):
        plan = self._plan([("1", "1"), ("2", "1")])
        self.assertEqual(len(plan.changed), 2)
        self.assertEqual(plan.changes[0].why, "disc 1 → —")

    def test_bare_one_is_kept_on_a_real_multi_disc_album(self):
        plan = self._plan([("1", "1"), ("1", "2")])
        self.assertIsNotNone(plan.message)
        self.assertIn("nothing to remove", plan.message or "")

    def test_a_track_with_no_disc_tag_is_listed_and_left_alone(self):
        # An untagged disc sorts as 0, so it leads the preview — look the row up
        # by path rather than assuming where the ordering puts it.
        plan = self._plan([("1", "1/1"), ("2", None)])
        by_path = {os.path.basename(c.path): c.why for c in plan.changes}
        self.assertEqual(len(plan.changes), 2)
        self.assertEqual(by_path["00.mp3"], "disc 1/1 → —")
        self.assertEqual(by_path["01.mp3"], "")

    def test_disc_two_of_three_is_kept_and_says_so(self):
        plan = self._plan([("1", "1/1"), ("1", "2/3")])
        kept = plan.changes[1]
        self.assertEqual(kept.why, "")
        self.assertEqual(kept.fields['keeps'], "2/3")


class StripLengthTagsTest(_Fixtures):
    def test_stale_frames_are_named_in_the_reason(self):
        paths = [_mp3(os.path.join(self.tmp, "a.mp3"), tlen=1000, tdly=500),
                 _mp3(os.path.join(self.tmp, "b.mp3"), tlen=1000),
                 _mp3(os.path.join(self.tmp, "c.mp3"))]
        songs, skipped = bo.read_length_tags(paths)
        plan = bo.plan_strip_length_tags(songs, skipped)
        self.assertEqual([c.why for c in plan.changes],
                         ["TLEN · TDLY", "TLEN", ""])

    def test_nothing_stale_says_so(self):
        songs, skipped = bo.read_length_tags(
            [_mp3(os.path.join(self.tmp, "a.mp3"))])
        plan = bo.plan_strip_length_tags(songs, skipped)
        self.assertEqual(plan.message, "No stale TLEN/TDLY tags found.")

    def test_the_writer_actually_deletes_both_frames(self):
        path = _mp3(os.path.join(self.tmp, "a.mp3"), tlen=1000, tdly=500)
        songs, skipped = bo.read_length_tags([path])
        plan = bo.plan_strip_length_tags(songs, skipped)
        bo.strip_length_writer(plan.changes[0])
        self.assertEqual(bo.tw.stale_length_tags(path), (False, False))

    def test_non_mp3_is_skipped_not_planned(self):
        m4a = os.path.join(self.tmp, "t.m4a")
        open(m4a, "wb").close()
        songs, skipped = bo.read_length_tags([m4a])
        self.assertEqual((songs, skipped), ([], 1))


class PictureTypeTest(_Fixtures):
    def test_only_mismatched_art_is_ticked(self):
        paths = [_mp3(os.path.join(self.tmp, "a.mp3"), pic_types=[0]),
                 _mp3(os.path.join(self.tmp, "b.mp3"), pic_types=[3])]
        art, skipped = bo.read_picture_types(paths)
        plan = bo.plan_set_picture_type(art, skipped, 3)
        self.assertEqual(len(plan.changed), 1)
        self.assertEqual(plan.changes[0].why, "Other → Cover (front)")
        self.assertEqual(plan.changes[1].why, "")

    def test_files_without_art_are_skipped_not_listed(self):
        paths = [_mp3(os.path.join(self.tmp, "a.mp3"), pic_types=[0]),
                 _mp3(os.path.join(self.tmp, "b.mp3"))]
        art, skipped = bo.read_picture_types(paths)
        self.assertEqual((len(art), skipped), (1, 1))

    def test_all_correct_already_says_so(self):
        art, skipped = bo.read_picture_types(
            [_mp3(os.path.join(self.tmp, "a.mp3"), pic_types=[3])])
        plan = bo.plan_set_picture_type(art, skipped, 3)
        self.assertEqual(plan.message, "Every image is already Cover (front).")

    def test_an_unnamed_type_falls_back_to_its_number(self):
        self.assertEqual(bo.picture_type_name(9), "type 9")


class ApplyChangesTest(_Fixtures):
    def setUp(self):
        super().setUp()
        self.paths = self._album([("1/2", "1/1"), ("2/2", "1/1")])
        ordered, skipped = bo.read_numbering(self.paths)
        self.plan = bo.plan_strip_single_disc(ordered, skipped)

    def test_writes_every_changed_file_by_default(self):
        applied = bo.apply_changes(
            self.plan, [], lambda c: bo.tw.clear_fields(c.path, {'disc'}))
        self.assertEqual((applied.written, applied.errors), (2, 0))
        self.assertTrue(all(bo.tw.read_number_pairs(p)['disc'] == ''
                            for p in self.paths))

    def test_honours_an_explicit_selection(self):
        applied = bo.apply_changes(
            self.plan, [], lambda c: bo.tw.clear_fields(c.path, {'disc'}),
            selected={self.paths[0]})
        self.assertEqual(applied.written, 1)
        self.assertEqual(bo.tw.read_number_pairs(self.paths[1])['disc'], '1')

    def test_a_raising_writer_counts_as_an_error_and_keeps_going(self):
        seen = []

        def _writer(change):
            seen.append(change.path)
            raise OSError("disk went away")

        applied = bo.apply_changes(self.plan, [], _writer)
        self.assertEqual((applied.written, applied.errors), (0, 2))
        self.assertEqual(len(seen), 2)

    def test_a_write_result_carrying_an_error_counts_as_one(self):
        class _Res:
            error = "nope"
        applied = bo.apply_changes(self.plan, [], lambda c: _Res())
        self.assertEqual((applied.written, applied.errors), (0, 2))

    def test_events_report_one_line_per_file(self):
        events = []
        bo.apply_changes(self.plan, [], lambda c: bo.tw.clear_fields(c.path, {'disc'}),
                         on_event=lambda kind, change, detail: events.append((kind, detail)))
        self.assertEqual(events, [("written", "disc 1/1 → —")] * 2)

    def test_the_plans_skipped_count_reaches_the_result(self):
        self.plan.skipped = 4
        applied = bo.apply_changes(self.plan, [], lambda c: None, selected=set())
        self.assertEqual(applied.skipped, 4)


class SummariseTest(unittest.TestCase):
    def test_counts_agree_with_their_nouns(self):
        self.assertEqual(bo.summarise(bo.Applied(written=1), "Renumbered"),
                         "Renumbered 1 file.")
        self.assertEqual(bo.summarise(bo.Applied(written=12), "Renumbered"),
                         "Renumbered 12 files.")

    def test_skipped_and_errors_are_appended_when_present(self):
        self.assertEqual(
            bo.summarise(bo.Applied(written=2, errors=1, skipped=3), "Reflowed"),
            "Reflowed 2 files. 3 unsupported skipped. 1 error.")

    def test_the_skipped_note_can_be_reworded(self):
        self.assertIn("without art or not MP3",
                      bo.summarise(bo.Applied(skipped=2), "Set",
                                   skipped_note="without art or not MP3"))


class RenameFilesTest(_Fixtures):
    def test_renames_and_follows_the_library_entry(self):
        a = _mp3(os.path.join(self.tmp, "a.mp3"), track="1")
        library = [{'path': a, 'title': 'A'}]
        final = os.path.join(self.tmp, "01 - A.mp3")
        applied = bo.rename_files([(a, final)], library)
        self.assertEqual((applied.written, applied.errors), (1, 0))
        self.assertTrue(os.path.exists(final))
        self.assertFalse(os.path.exists(a))
        self.assertEqual(library[0]['path'], final)

    def test_a_swap_does_not_clobber_either_file(self):
        # The two-phase move exists for exactly this: each target is the other
        # file's current name, so a naive rename would destroy one of them.
        a = _mp3(os.path.join(self.tmp, "a.mp3"), track="1")
        b = _mp3(os.path.join(self.tmp, "b.mp3"), track="2")
        applied = bo.rename_files([(a, b), (b, a)], [])
        self.assertEqual((applied.written, applied.errors), (2, 0))
        self.assertEqual(bo.tw.read_number_pairs(a)['track'], '2')
        self.assertEqual(bo.tw.read_number_pairs(b)['track'], '1')

    def test_a_failed_rename_is_counted_and_the_rest_still_run(self):
        a = _mp3(os.path.join(self.tmp, "a.mp3"), track="1")
        b = _mp3(os.path.join(self.tmp, "b.mp3"), track="2")
        nowhere = os.path.join(self.tmp, "no", "such", "dir", "x.mp3")
        applied = bo.rename_files([(a, nowhere),
                                   (b, os.path.join(self.tmp, "ok.mp3"))], [])
        self.assertEqual((applied.written, applied.errors), (1, 1))
        # The failed one rolled back rather than being left under a dot-name.
        self.assertTrue(os.path.exists(a))
        self.assertEqual([n for n in os.listdir(self.tmp) if n.startswith('.rn_')], [])


class ApplyCoversTest(_Fixtures):
    def _jpeg(self) -> str:
        path = os.path.join(self.tmp, "cover.jpg")
        with open(path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9")
        return path

    def test_embeds_the_image(self):
        track = _mp3(os.path.join(self.tmp, "a.mp3"))
        applied = bo.apply_covers({track: self._jpeg()}, [])
        self.assertEqual((applied.written, applied.errors), (1, 0))
        self.assertTrue(bo.tw.has_cover(track))

    def test_fill_blanks_keeps_existing_art_and_counts_it(self):
        track = _mp3(os.path.join(self.tmp, "a.mp3"), pic_types=[3])
        applied = bo.apply_covers({track: self._jpeg()}, [], overwrite=False)
        self.assertEqual((applied.written, applied.kept), (0, 1))

    def test_an_unreadable_image_is_an_error_not_a_write(self):
        track = _mp3(os.path.join(self.tmp, "a.mp3"))
        missing = os.path.join(self.tmp, "gone.jpg")
        applied = bo.apply_covers({track: missing}, [])
        self.assertEqual((applied.written, applied.errors), (0, 1))

    def test_tracks_with_no_matched_image_are_passed_over(self):
        track = _mp3(os.path.join(self.tmp, "a.mp3"))
        applied = bo.apply_covers({track: None}, [])
        self.assertEqual((applied.written, applied.errors), (0, 0))

    def test_an_unselected_track_is_not_written(self):
        track = _mp3(os.path.join(self.tmp, "a.mp3"))
        applied = bo.apply_covers({track: self._jpeg()}, [], selected=set())
        self.assertEqual(applied.written, 0)
        self.assertFalse(bo.tw.has_cover(track))


class ApplyFrameWritesTest(_Fixtures):
    def test_writes_a_frame_and_replaces_an_existing_one(self):
        path = _mp3(os.path.join(self.tmp, "a.mp3"))
        bo.apply_frame_writes({path: [('TIT2', 'First')]}, [])
        bo.apply_frame_writes({path: [('TIT2', 'Second')]}, [])
        self.assertEqual(str(ID3(path)['TIT2'].text[0]), 'Second')

    def test_fill_blanks_leaves_an_existing_value_alone(self):
        path = _mp3(os.path.join(self.tmp, "a.mp3"))
        bo.apply_frame_writes({path: [('TIT2', 'First')]}, [])
        applied = bo.apply_frame_writes({path: [('TIT2', 'Second')]}, [],
                                        overwrite=False)
        self.assertEqual((applied.written, applied.kept), (0, 1))
        self.assertEqual(str(ID3(path)['TIT2'].text[0]), 'First')

    def test_a_file_that_cannot_be_written_counts_as_an_error(self):
        applied = bo.apply_frame_writes(
            {os.path.join(self.tmp, "no", "a.mp3"): [('TIT2', 'x')]}, [])
        self.assertEqual((applied.written, applied.errors), (0, 1))

    def test_writes_several_frames_to_one_file(self):
        path = _mp3(os.path.join(self.tmp, "a.mp3"))
        bo.apply_frame_writes({path: [('TSOP', 'Wren, DJ'), ('TSOA', 'Album, The')]}, [])
        tags = ID3(path)
        self.assertEqual(str(tags['TSOP'].text[0]), 'Wren, DJ')
        self.assertEqual(str(tags['TSOA'].text[0]), 'Album, The')


class DeriveTest(_Fixtures):
    def test_derives_a_title_from_the_file_name(self):
        path = _mp3(os.path.join(self.tmp, "01 - Hungry Like the Wolf.mp3"))
        plan, derived = bo.plan_derive([path], {'title'})
        self.assertEqual(plan.changes[0].fields['title'], "Hungry Like the Wolf")
        self.assertIn(path, derived)

    def test_fill_blanks_declines_a_field_that_is_already_set(self):
        path = os.path.join(self.tmp, "01 - Wolf.mp3")
        _mp3(path)
        audio = ID3(path)
        from mutagen.id3 import TIT2  # type: ignore[reportPrivateImportUsage]
        audio.add(TIT2(encoding=3, text=["Already"]))
        audio.save(path, v2_version=3)
        plan, _derived = bo.plan_derive([path], {'title'}, overwrite=False)
        self.assertIsNotNone(plan.message)
        self.assertIn("already set", plan.message or "")

    def test_overwrite_offers_it_anyway(self):
        path = os.path.join(self.tmp, "01 - Wolf.mp3")
        _mp3(path)
        audio = ID3(path)
        from mutagen.id3 import TIT2  # type: ignore[reportPrivateImportUsage]
        audio.add(TIT2(encoding=3, text=["Already"]))
        audio.save(path, v2_version=3)
        plan, _derived = bo.plan_derive([path], {'title'}, overwrite=True)
        self.assertEqual(plan.changes[0].fields['title'], "Wolf")

    def test_the_writer_actually_writes_the_derivation(self):
        path = _mp3(os.path.join(self.tmp, "01 - Wolf.mp3"))
        plan, derived = bo.plan_derive([path], {'title'})
        applied = bo.apply_changes(plan, [], bo.derive_writer(derived, {'title'}, False))
        self.assertEqual(applied.written, 1)
        self.assertEqual(str(ID3(path)['TIT2'].text[0]), "Wolf")

    def test_unwritable_formats_are_counted_not_derived(self):
        odd = os.path.join(self.tmp, "notes.txt")
        open(odd, "w").close()
        plan, _derived = bo.plan_derive([odd], {'title'})
        self.assertEqual(plan.skipped, 1)
        self.assertEqual(plan.message, "No MP3/MP4 tracks to derive from.")

    def test_sort_orders_ride_along_when_asked_for(self):
        vals = bo.augment_sort({'artist': 'DJ Wren'}, {'artist', 'sort'})
        self.assertEqual(vals['artist_sort'], 'Wren, DJ')

    def test_sort_orders_are_not_invented_for_a_derived_name(self):
        vals = bo.augment_sort({'album_artist': 'Various Artists'},
                               {'album_artist', 'sort'})
        self.assertNotIn('album_artist_sort', vals)


if __name__ == "__main__":
    unittest.main()
