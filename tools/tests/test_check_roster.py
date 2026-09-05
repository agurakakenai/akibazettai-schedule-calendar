"""Offline roster regressions: python -m unittest discover -s tools/tests."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock
import urllib.error


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_roster', TOOLS / 'check-roster.py')
check_roster = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_roster)


class ParseSiteTests(unittest.TestCase):
    def test_single_quotes_added_attributes_and_nested_spans(self):
        html = """
        <ul>
          <li class="shopNum01"><a href="/maid/a"><p>ひかり</p></a></li>
          <li data-new="true" class='featured shopNum02 newcomer'>
            <a href='/maid/new'><figure><img alt='wrong name'></figure>
              <p class='name'><span>監査用<span>新人</span></span></p>
            </a>
          </li>
          <li class=shopNum03 data-extra=value><p><span>あ</span><span>む</span></p></li>
          <li id='last' class='shopNum04'><p data-label='name'>  ゆめ  </p></li>
        </ul>
        """
        self.assertEqual(check_roster.parse_site(html),
                         [('ひかり', 's1'), ('監査用新人', 's2'), ('あむ', 's3'), ('ゆめ', 's4')])

    def test_additional_li_attribute_does_not_hide_newcomer(self):
        for attrs in ('class="shopNum02"', 'class="shopNum02" data-new="true"',
                      "data-new='true' class='shopNum02'"):
            with self.subTest(attrs=attrs):
                self.assertEqual(check_roster.parse_site(f'<li {attrs}><p>新人</p></li>'),
                                 [('新人', 's2')])

    def test_duplicate_tabs_keep_first_occurrence_and_publication_order(self):
        html = ('<li class="shopNum02"><p>まひろ</p></li>'
                '<li class="shopNum01"><p>ひかり</p></li>'
                '<li class="shopNum04"><p>まひろ</p></li>'
                '<li class="shopNum03"><p>あむ</p><p>紹介文</p></li>')
        self.assertEqual(check_roster.parse_site(html),
                         [('まひろ', 's2'), ('ひかり', 's1'), ('あむ', 's3')])

    def test_unrelated_classes_and_nested_list_names_are_not_staff(self):
        html = ('<li class="notshopNum01"><p>無関係</p></li>'
                '<li class="shopNum02extra"><p>無関係</p></li>'
                '<li class="shopNum01"><ul><li><p>ナビ</p></li></ul><p>ひかり</p></li>'
                '<p>外側</p><li><p>一覧</p></li>')
        self.assertEqual(check_roster.parse_site(html), [('ひかり', 's1')])

    def test_entities_and_line_breaks_inside_name_paragraph(self):
        html = "<LI CLASS='shopNum02'><P data-name='true'>\n<span>ま&#12402;ろ</span>\n</P></LI>"
        self.assertEqual(check_roster.parse_site(html), [('まひろ', 's2')])

    def test_empty_names_and_empty_html_do_not_create_entries(self):
        for html in ('', '<p>名簿ではありません</p>',
                     '<li class="shopNum01"><p><span> </span></p></li>',
                     '<li class="shopNum01"><img alt="名前"></li>'):
            with self.subTest(html=html):
                self.assertEqual(check_roster.parse_site(html), [])

    def test_empty_decoration_paragraph_does_not_hide_following_name(self):
        html = '<li class="shopNum01"><p> </p><p><span>ひかり</span></p></li>'
        self.assertEqual(check_roster.parse_site(html), [('ひかり', 's1')])


class MainTests(unittest.TestCase):
    def run_main(self, html=None, error=None):
        output = io.StringIO()
        schedule = (['ひかり'], {'ひかり': 's1'}, [])
        with (mock.patch.object(check_roster, 'fetch', return_value=html, side_effect=error),
              mock.patch.object(check_roster, 'read_schedule', return_value=schedule) as read,
              contextlib.redirect_stdout(output)):
            code = check_roster.main()
        return code, output.getvalue(), read

    def test_newcomer_with_extra_attributes_is_reported(self):
        html = ('<li class="shopNum01"><p>ひかり</p></li>'
                '<li class="shopNum02" data-new="true"><p><span>監査用新人</span></p></li>')
        code, output, _ = self.run_main(html)
        self.assertEqual(code, 1)
        self.assertIn('roster にいません', output)
        self.assertIn('監査用新人', output)

    def test_matching_roster_still_succeeds(self):
        code, output, _ = self.run_main("<li data-staff='1' class='shopNum01'><p>ひかり</p></li>")
        self.assertEqual(code, 0)
        self.assertIn('差分はありません。', output)

    def test_empty_parsing_is_an_explicit_error(self):
        for html in ('', '<ul><li><p>ひかり</p></li></ul>'):
            with self.subTest(html=html):
                code, output, read = self.run_main(html)
                self.assertEqual(code, 2)
                self.assertIn('掲載を読み取れませんでした', output)
                read.assert_not_called()

    def test_failed_fetch_is_an_explicit_error(self):
        code, output, read = self.run_main(error=urllib.error.URLError('offline'))
        self.assertEqual(code, 2)
        self.assertIn('公式サイトを取得できませんでした', output)
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
