<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings\AppInfo;

use OCA\Files\Event\LoadSidebar;
use OCA\RecognizeLlmSettings\Capabilities;
use OCA\RecognizeLlmSettings\Listener\LoadSidebarListener;
use OCP\AppFramework\App;
use OCP\AppFramework\Bootstrap\IBootContext;
use OCP\AppFramework\Bootstrap\IBootstrap;
use OCP\AppFramework\Bootstrap\IRegistrationContext;

class Application extends App implements IBootstrap {
	public const APP_ID = 'recognize_llm_settings';
	/** Must match settings_ui.SECTION_ID in the ExApp. */
	public const SECTION_ID = 'recognize_llm';

	public function __construct() {
		parent::__construct(self::APP_ID);
	}

	public function register(IRegistrationContext $context): void {
		$context->registerCapability(Capabilities::class);
		$context->registerEventListener(LoadSidebar::class, LoadSidebarListener::class);
	}

	public function boot(IBootContext $context): void {
	}
}
