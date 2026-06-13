# brianking.org

Static site for `brianking.org`, designed to run on GitHub Pages.

## Local preview

From this folder:

```sh
python3 -m http.server 8080
```

Then open `http://localhost:8080`.

## GitHub Pages

1. Create a GitHub repository for the site.
2. Commit these files and push them to the default branch.
3. In the repository settings, enable GitHub Pages from the root of the default branch.
4. Point the domain DNS at GitHub Pages and keep the `CNAME` file as `brianking.org`.

The site uses static HTML and CSS only, so there is no build step.
