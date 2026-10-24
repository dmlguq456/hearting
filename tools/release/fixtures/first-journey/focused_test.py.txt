import unittest

from greet import greet


class GreetTest(unittest.TestCase):
    def test_greet_says_hello(self):
        self.assertEqual(greet(), "Hello")


if __name__ == "__main__":
    unittest.main()
