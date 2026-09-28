import unittest
from transcript_utils import canonical_tv_show_name, identifytv_failure_note


class TranscriptUtilsTests(unittest.TestCase):
    def test_canonical_tv_show_name_removes_season_and_disc_suffixes(self):
        cases = {
            'Homeland S01': 'Homeland',
            'Homeland S02': 'Homeland',
            'The Sopranos S06 disc4': 'The Sopranos',
            'The Sopranos S06 S06': 'The Sopranos',
            'The Sopranos': 'The Sopranos',
            '24': '24',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(canonical_tv_show_name(source), expected)

    def test_failure_note_explains_missing_reference_episodes(self):
        output = "! could not reach TVmaze for 'Homeland S01': HTTP Error 404\nno reference episodes — check the show name"
        self.assertEqual(
            identifytv_failure_note(1, output),
            'Transcript verification failed: TVmaze found no episodes. Use only the series title in Show (for example, “Homeland”), with the season in the separate Season field.'
        )

    def test_failure_note_does_not_echo_arbitrary_subprocess_output(self):
        self.assertEqual(
            identifytv_failure_note(2, 'token=secret-value arbitrary failure'),
            'Transcript verification failed (identifytv exit 2). Check the service log for the safe failure classification.'
        )


if __name__ == '__main__':
    unittest.main()
