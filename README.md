# bunny-deploy

GitHub Action that uploads a repo (or its build output) to a **Bunny Storage**
zone and purges the CDN, so a Bunny pull zone can serve files straight from
storage instead of pulling from another host.

Only one secret is needed: the Bunny **account API key** (`BUNNY_API_KEY`).
The storage zone's password and storage host are looked up through the Bunny
API at run time and masked in the logs.

## Use

```yaml
# .github/workflows/deploy-bunny.yml
name: Deploy to Bunny
on:
  push: { branches: [main] }
  workflow_dispatch: {}
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: certifiedcode/bunny-deploy@v1
        with:
          storage-zone: my-zone
          public-url: https://cdn.example.com
          # build: npm ci && npm run build
          # dir: dist
        env:
          BUNNY_API_KEY: ${{ secrets.BUNNY_API_KEY }}
```

| Input | Default | |
|---|---|---|
| `storage-zone` | required | Bunny Storage zone name |
| `public-url` | `""` | Pull zone URL, used to purge changed files |
| `build` | `""` | Command to run before uploading |
| `dir` | `.` | Folder to upload. `.` = the repo's git-tracked files |
| `full` | `auto` | `true` uploads everything; `auto` = everything on manual runs, changed files on pushes |
| `api-key` | `""` | Account API key, if not given as `BUNNY_API_KEY` |

### Modes

- **Source mode** (`dir: .`): pushes upload/delete only the files changed in
  the push and purge those URLs. Manual runs upload every tracked file and
  purge nothing (safe for a first sync before switching a pull zone's origin).
  `.github/`, `.gitignore` and `.gitattributes` are never uploaded.
- **Build mode** (any other `dir`): uploads every file in the folder and purges
  the pull zone whose hostname matches `public-url`. Files removed from the
  build stay in storage, so use versioned/hashed file names.

## Connect a repo

1. Create a Storage zone in Bunny and point the pull zone's origin at it
   (Pull zone → Origin → Storage Zone) once the first sync looks right.
2. Add `BUNNY_API_KEY` as an organisation secret (or repository secret).
3. Add the workflow above and run it once manually.
