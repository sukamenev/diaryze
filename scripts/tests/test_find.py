"""Tests for read-only transcript search and meaningful word counting."""

from pathlib import Path
import tempfile
import unittest

from scripts.find import meaningful_speech_stats, search_transcripts


class FindTranscriptsTests(unittest.TestCase):
    # Check that word counts exclude transcript markup and non-speech annotations.
    def test_meaningful_word_count_ignores_markers_timestamps_and_notes(self):
        body = """## Речь

[00:00:00.50] **Сергей** Алло? [шум]
[00:00:01.20] **Автоответчик** [неразборчиво] [возможно: перезвоните позже]

## Заметки

Много лишних слов в заметках не должны считаться.
"""
        words, lines, no_speech = meaningful_speech_stats(body)
        self.assertEqual((words, lines, no_speech), (3, 2, False))

        empty_body = "## Речь\n\n[без речи]\n"
        self.assertEqual(meaningful_speech_stats(empty_body), (0, 0, True))

    # Check that search applies duration, speech, spam, participant, and date filters.
    def test_filters_by_duration_words_spam_bot_person_and_date(self):
        with tempfile.TemporaryDirectory(prefix="find-md-test-") as tmp:
            root = Path(tmp)
            d25 = root / "00_Diaryze_Data/Text/Phone/2025"
            d26 = root / "00_Diaryze_Data/Text/Phone/2026"
            d25.mkdir(parents=True)
            d26.mkdir(parents=True)
            (root / "Phone").mkdir()
            (root / "Phone/.owner").write_text("type: phone\nowner: ivanov_sergey\n")

            (d25 / "short_bot.m4a.md").write_text(
                "---\nschema_version: '1.0'\nduration_s: 12.50\nspam_score: 0.85\n"
                "identification: unresolved\nrecorded_at: '2025-12-01T10:00:00'\n"
                "direction: in\nphones: ['+70000000109']\n"
                "speakers:\n  Сергей: {person: ivanov_sergey, source: filename}\n"
                "  Автоответчик: {person: null, temp: true}\n---\n\n"
                "[00:00:01.00] **Сергей** Алло?\n[00:00:02.00] **Автоответчик** [неразборчиво]\n",
                encoding="utf-8",
            )
            (d26 / "long_friend.m4a.md").write_text(
                "---\nschema_version: '1.0'\nduration_s: 180.00\nspam_score: null\n"
                "identification: complete\nrecorded_at: '2026-01-15T18:30:00'\n"
                "direction: out\ncontact: Виктория Орлова\nphones: ['+70000000101']\n"
                "speakers:\n  Сергей: {person: ivanov_sergey, source: speech}\n"
                "  Виктория: {person: orlova_viktoria, source: speech}\n---\n\n"
                "[00:00:01.00] **Сергей** Привет, как дела сегодня вечером?\n"
                "[00:00:03.00] **Виктория** Всё отлично, собираемся гулять.\n",
                encoding="utf-8",
            )

            self.assertEqual(len(search_transcripts(root, max_sec=30)), 1)
            self.assertEqual(len(search_transcripts(root, max_words=2)), 1)
            self.assertEqual(len(search_transcripts(root, min_spam=0.7)), 1)
            self.assertEqual(len(search_transcripts(root, only_bot=True)), 1)
            self.assertEqual(search_transcripts(root, person="orlova_viktoria")[0]["md"], "00_Diaryze_Data/Text/Phone/2026/long_friend.m4a.md")
            self.assertEqual(len(search_transcripts(root, folder="Phone/2025")), 1)
            self.assertEqual(len(search_transcripts(root, direction="in")), 1)
            self.assertEqual(len(search_transcripts(root, text_query="гулять")), 1)


if __name__ == "__main__":
    unittest.main()
