.PHONY: help test version version-patch version-minor version-major _bump-version
.SILENT:

help: ## | List all available commands
	echo "Available commands in this makefile"
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	| sed -n 's/^\(.*\): \(.*\)##\(.*\)/\1\3/p' \
	| column -t -s '|'

test: ## | Check the panoply-lib copies, then run the offline selftest
	../panoply-lib/sync.sh --check augur
	python3 src/augur/augur.py selftest

version-patch: ## | Release the next PATCH version: stamp, test, tag, push, move the formula
	@$(MAKE) _bump-version BUMP=patch

version-minor: ## | Release the next MINOR version: stamp, test, tag, push, move the formula
	@$(MAKE) _bump-version BUMP=minor

version-major: ## | Release the next MAJOR version: stamp, test, tag, push, move the formula
	@$(MAKE) _bump-version BUMP=major

version: version-patch ## | Alias for version-patch (the common release)

# `__version__` in augur.py is the one home (pyproject.toml reads it at build time),
# so the next version is computed from it and handed to scripts/release.sh, which
# owns every step after that and refuses a dirty tree or an existing tag.
_bump-version:
	@NEXT=$$(python3 -c "import re,sys; \
		v=re.search(r'^__version__ = \"(\d+)\.(\d+)\.(\d+)\"', open('src/augur/augur.py').read(), re.M); \
		M,m,p=map(int,v.groups()); \
		print({'major':f'{M+1}.0.0','minor':f'{M}.{m+1}.0','patch':f'{M}.{m}.{p+1}'}['$(BUMP)'])") && \
		echo "--- Releasing $$NEXT ($(BUMP)) ---" && \
		scripts/release.sh "$$NEXT"
