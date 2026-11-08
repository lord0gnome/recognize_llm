<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings;

use OCA\RecognizeLlmSettings\AppInfo\Application;
use OCP\Capabilities\IPublicCapability;

/**
 * Tells the ExApp that the dedicated section exists, so it registers its forms there instead of
 * in "Additional settings" (settings_ui.target_section).
 *
 * Public on purpose: ExApp requests carry no user session, and Nextcloud only returns public
 * capabilities to those. It reveals nothing but the existence of this settings page.
 */
class Capabilities implements IPublicCapability {
	/**
	 * @return array{recognize_llm_settings: array{admin_section: string}}
	 */
	public function getCapabilities(): array {
		return [
			Application::APP_ID => [
				'admin_section' => Application::SECTION_ID,
			],
		];
	}
}
