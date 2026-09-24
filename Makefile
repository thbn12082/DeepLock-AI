PYTHON ?= python
CONTENT := $(PYTHON) -m deeplock_content.cli
WORKDIR ?= .build/content
INPUT ?= fixtures/gradient_descent.md
MODEL ?= gpt-5.5
REVIEW_MODEL ?= gpt-5.5
LEGACY_CONTRACT_FLAG ?= --allow-legacy-no-contract

.PHONY: content-inspect content-generate content-validate content-ai-review content-package content-install android-test android-debug android-release test

content-inspect:
	cd content_builder && $(CONTENT) inspect --input ../$(INPUT) --workdir ../$(WORKDIR)

content-generate: content-inspect
	cd content_builder && $(CONTENT) generate --workdir ../$(WORKDIR) --model $(MODEL) --prompt-version course-v2 --resume

content-validate:
	cd content_builder && $(CONTENT) validate --workdir ../$(WORKDIR) --report ../.build/reports/content-validation.json $(LEGACY_CONTRACT_FLAG)

content-ai-review:
	cd content_builder && $(CONTENT) ai-review --workdir ../$(WORKDIR) --model $(REVIEW_MODEL) --prompt-version reviewer-v2 --max-repair-rounds 2 --resume --report ../.build/reports/ai-review.json $(LEGACY_CONTRACT_FLAG)

content-package:
	cd content_builder && $(CONTENT) package --workdir ../$(WORKDIR) --output ../dist/default.dlpack $(LEGACY_CONTRACT_FLAG)

content-install:
	cd content_builder && $(CONTENT) install-android --pack ../dist/default.dlpack --android-assets ../android/app/src/main/assets/content

android-test:
	cd android && ./gradlew test

android-debug:
	cd android && ./gradlew verifyNoNetworkPermission verifyNoNetworkDependencies verifyBundledPack assembleDebug

android-release:
	cd android && ./gradlew verifyNoNetworkPermission verifyNoNetworkDependencies verifyBundledPack testDebugUnitTest assembleRelease

test:
	cd content_builder && $(PYTHON) -m pytest
