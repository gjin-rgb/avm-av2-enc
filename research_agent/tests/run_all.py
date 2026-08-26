"""Entry point for build systems that need a single test main."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == "__main__":
  loader = unittest.TestLoader()
  suite = loader.discover(os.path.dirname(os.path.abspath(__file__)))
  result = unittest.TextTestRunner(verbosity=2).run(suite)
  sys.exit(0 if result.wasSuccessful() else 1)
