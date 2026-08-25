FROXX MASTER FIX

Wispbyte startup command:
sh /home/container/startup.sh

The database file is created automatically. Existing froxx.sqlite3 is never deleted by the launcher.
The launcher restores main.py/database.py only when the source file is missing.
The original Errno 28 is an environment storage/inode error during pip installation; code cannot override a hard quota.
