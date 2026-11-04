"""Run SavabYar as the only bot process on Runflare."""

import os
import sys


def main():
    os.execv(sys.executable, [sys.executable, 'savabyar/bot.py'])


if __name__ == '__main__':
    main()
