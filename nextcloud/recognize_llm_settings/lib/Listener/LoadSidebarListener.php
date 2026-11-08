<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings\Listener;

use OCA\Files\Event\LoadSidebar;
use OCA\RecognizeLlmSettings\AppInfo\Application;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Util;

/**
 * Files and Memories both dispatch LoadSidebar on their pages; load the description UI there.
 *
 * @template-implements IEventListener<Event>
 */
class LoadSidebarListener implements IEventListener {
	public function handle(Event $event): void {
		if (!$event instanceof LoadSidebar) {
			return;
		}
		Util::addScript(Application::APP_ID, 'description');
		Util::addStyle(Application::APP_ID, 'description');
	}
}
