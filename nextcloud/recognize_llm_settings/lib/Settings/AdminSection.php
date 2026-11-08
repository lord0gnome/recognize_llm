<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings\Settings;

use OCA\RecognizeLlmSettings\AppInfo\Application;
use OCP\IURLGenerator;
use OCP\Settings\IIconSection;

/**
 * The "Recognize LLM" administration section. Its id is what the ExApp's declarative forms use
 * as section_id (settings_ui.SECTION_ID); the section only shows once a form targets it.
 */
class AdminSection implements IIconSection {
	public function __construct(
		private IURLGenerator $urlGenerator,
	) {
	}

	public function getID(): string {
		return Application::SECTION_ID;
	}

	public function getName(): string {
		return 'Recognize LLM';
	}

	public function getPriority(): int {
		return 75;
	}

	public function getIcon(): string {
		return $this->urlGenerator->imagePath(Application::APP_ID, 'app-dark.svg');
	}
}
