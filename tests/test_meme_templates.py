import ast
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from src.render.meme_templates import render_news, render_sold_out
from src.common.utils import ROOT


class MemeTemplatesTests(unittest.TestCase):
    def test_news(self):
        for good, size in [(True, (600, 450)), (False, (600, 425))]:
            with self.subTest(good=good):
                data = render_news('三理生日快乐\n明天也要开心', good=good)
                with Image.open(BytesIO(data)) as im:
                    self.assertEqual(im.size, size)
                    self.assertEqual(im.format, 'PNG')

    def test_news_limits(self):
        for text in ['', '  ', '啊' * 201, '啊\n' * 80]:
            with self.subTest(text=text[:5]), self.assertRaises(ValueError):
                render_news(text, good=True)

    def test_sold_limits_and_static_output(self):
        with tempfile.TemporaryDirectory() as folder:
            for size in [(100, 100), (2000, 20), (20, 2000)]:
                p = Path(folder) / 'source.png'
                Image.new('RGB', size, 'white').save(p)
                with Image.open(BytesIO(render_sold_out(p))) as im:
                    self.assertLessEqual(max(im.size), 1600)
                    self.assertEqual(im.n_frames, 1)

    def test_registration_and_help(self):
        tree = ast.parse((ROOT/'src/plugins/command_registry.py').read_text())
        names = {node.args[0].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == 'on_command' and node.args
                 and isinstance(node.args[0], ast.Constant)}
        self.assertTrue({'喜报', '悲报', '卖掉了'} <= names)
        source = (ROOT/'src/render/help.py').read_text()
        self.assertIn('"title": "表情制作"', source)
