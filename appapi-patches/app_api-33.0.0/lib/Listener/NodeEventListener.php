<?php

declare(strict_types=1);

/**
 * SPDX-FileCopyrightText: 2024 Nextcloud GmbH and Nextcloud contributors
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

namespace OCA\AppAPI\Listener;

use OCA\AppAPI\Service\EventsListenerService;
use OCP\EventDispatcher\Event;
use OCP\EventDispatcher\IEventListener;
use OCP\Files\Events\Node\NodeCreatedEvent;
use OCP\Files\Events\Node\NodeWrittenEvent;
use OCP\Files\File;
use OCP\Files\Node;
use Psr\Log\LoggerInterface;

/**
 * Dispatches NC file-system events (NodeCreated / NodeWritten) to any exApp
 * that registered itself via POST /api/v1/events_listener.
 *
 * @template-implements IEventListener<NodeCreatedEvent|NodeWrittenEvent>
 */
class NodeEventListener implements IEventListener {
	public function __construct(
		private readonly EventsListenerService $eventsListenerService,
		private readonly LoggerInterface       $logger,
	) {
	}

	public function handle(Event $event): void {
		if ($event instanceof NodeCreatedEvent) {
			$subtype = 'NodeCreatedEvent';
			$node = $event->getNode();
		} elseif ($event instanceof NodeWrittenEvent) {
			$subtype = 'NodeWrittenEvent';
			$node = $event->getNode();
		} else {
			return;
		}

		$eventData = $this->buildEventData($node);
		if ($eventData === null) {
			return;
		}

		$this->eventsListenerService->dispatchEvent('node_event', $subtype, $eventData);
	}

	private function buildEventData(Node $node): ?array {
		// Extract userId and relative path from the internal path /userId/files/path/to/file
		$fullPath = $node->getPath();
		if (!preg_match('/^\/([^\/]+)\/files(.*)$/', $fullPath, $m)) {
			return null;
		}
		$userId   = $m[1];
		$relPath  = $m[2] !== '' ? $m[2] : '/';
		$name      = $node->getName();
		$directory = rtrim(dirname($relPath), '/') ?: '/';

		return [
			'target' => [
				'fileId'      => $node->getId(),
				'name'        => $name,
				'directory'   => $directory,
				'etag'        => $node->getEtag(),
				'mime'        => $node->getMimeType(),
				'fileType'    => ($node instanceof File) ? 'file' : 'dir',
				'size'        => $node->getSize(),
				'favorite'    => 'false',
				'permissions' => $node->getPermissions(),
				'mtime'       => $node->getMTime(),
				'userId'      => $userId,
			],
		];
	}
}
