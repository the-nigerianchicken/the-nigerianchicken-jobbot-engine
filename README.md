# jobbot engine

The engine of a private app that finds internship postings: it checks job boards, judges what changed by its
owner's rules, and reports to the app. This repo is a copy of the engine's code, published by the app for
GitHub to run; it holds no data. Changes are made in the private repo, so pull requests here aren't read.

Each branch is one instance: `staging` and `production`. Production's engine runs on its own server and
follows the `production` branch; GitHub runs staging's runs, and production's resumes. A run is started by the app (`engine.yml`), takes
its state from the app and gives it back, and keeps its log there, out of this repo's public run pages.
