<?php

declare(strict_types=1);

namespace OCA\RecognizeLlmSettings\Service;

use OCP\Files\Folder;
use OCP\Files\IRootFolder;
use OCP\Files\Node;
use OCP\IDBConnection;
use Psr\Log\LoggerInterface;

/**
 * Reads the description Recognize LLM wrote for a file.
 *
 * The ExApp stores it as the WebDAV dead property {http://owncloud.org/ns}note, written in the
 * file OWNER's context (lib/dav.py set_nc_note). Nextcloud's CustomPropertiesBackend keeps such
 * properties in oc_properties keyed by (userid, propertypath = "files/<uid>/<path>"), and moves
 * them along on rename/move. Nothing in Files or Memories displays that property, hence this.
 */
class DescriptionService {
	public const PROPERTY = '{http://owncloud.org/ns}note';

	public function __construct(
		private IRootFolder $rootFolder,
		private IDBConnection $db,
		private LoggerInterface $logger,
	) {
	}

	/**
	 * @return string|null|false the description; null when the file has none yet; false when
	 *                           the file does not exist or is not accessible to $userId
	 */
	public function forUser(string $userId, int $fileId): string|null|false {
		$userFolder = $this->rootFolder->getUserFolder($userId);
		$node = $userFolder->getFirstNodeById($fileId);
		if ($node === null) {
			return false;
		}

		// Owner first (that is where the ExApp writes); the viewer's own path second, which covers
		// storages without an owner (group folders, external storage) where the ExApp ran as them.
		$candidates = [];
		$owner = $node->getOwner()?->getUID();
		if ($owner !== null && $owner !== $userId) {
			try {
				$ownerNode = $this->rootFolder->getUserFolder($owner)->getFirstNodeById($fileId);
				if ($ownerNode !== null) {
					$candidates[] = [$owner, $this->davPath($owner, $this->rootFolder->getUserFolder($owner), $ownerNode)];
				}
			} catch (\Throwable $e) {
				$this->logger->debug('recognize_llm_settings: owner lookup failed', ['exception' => $e]);
			}
		}
		$candidates[] = [$userId, $this->davPath($userId, $userFolder, $node)];

		foreach ($candidates as [$propUser, $path]) {
			if ($path === null) {
				continue;
			}
			$value = $this->read($propUser, $path);
			if ($value !== null && trim($value) !== '') {
				return trim($value);
			}
		}
		return null;
	}

	private function davPath(string $uid, Folder $userFolder, Node $node): ?string {
		$relative = $userFolder->getRelativePath($node->getPath());
		if ($relative === null) {
			return null;
		}
		$path = 'files/' . $uid . $relative;
		// Mirrors CustomPropertiesBackend::formatPath.
		return strlen($path) > 250 ? sha1($path) : $path;
	}

	private function read(string $uid, string $path): ?string {
		$qb = $this->db->getQueryBuilder();
		$qb->select('propertyvalue')
			->from('properties')
			->where($qb->expr()->eq('userid', $qb->createNamedParameter($uid)))
			->andWhere($qb->expr()->eq('propertypath', $qb->createNamedParameter($path)))
			->andWhere($qb->expr()->eq('propertyname', $qb->createNamedParameter(self::PROPERTY)))
			->setMaxResults(1);
		$result = $qb->executeQuery();
		$value = $result->fetchOne();
		$result->closeCursor();
		return $value === false ? null : (string)$value;
	}
}
