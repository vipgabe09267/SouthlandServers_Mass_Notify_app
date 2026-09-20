import unittest
from sls_windowing import window_rectangle


class WorkAreaTests(unittest.TestCase):
    def test_small_scaled_secondary_and_taskbar_areas_contain_entire_window(self):
        for area in ((0, 0, 1280, 672), (0, 40, 800, 600), (-1280, -720, 0, -48), (60, 0, 1920, 1040)):
            with self.subTest(area=area):
                width, height, x, y = window_rectangle(area, (1140, 840))
                self.assertGreaterEqual(x, area[0])
                self.assertGreaterEqual(y, area[1])
                self.assertLessEqual(x+width+16, area[2])
                self.assertLessEqual(y+height+48, area[3])
                self.assertLessEqual(abs((x+width+16-area[0]) - (area[2]-x)), 1)
