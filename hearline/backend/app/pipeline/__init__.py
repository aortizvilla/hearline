"""Hearline content pipeline: interests in, daily MP3 episode out. Files run in number order.

    step1_topics.py    selected topic → classified shared Topic (when a user selects a topic)
    step2_research.py  web search → validated Story rows       (once per topic per day)
    step3_scripts.py   write → verify → speech → StoryClip     (once per story x voice setting)
    step4_assemble.py  pick unseen stories, run steps 5-6      (once per user per day)
    step5_phrases.py   greeting, transitions, outro → speech   (once per phrase x voice setting)
    step6_audio.py     join + level audio → MP3 + tags         (once per episode)

See README.md in this folder. When steps run: app.jobs. Services used: app.providers.
"""
