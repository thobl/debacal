<?php
/** Optional independent validator; never loaded by the Debacal application.
 *
 * Usage: php validate_caldav.php /path/to/vendor/autoload.php
 * Input: JSON object of opaque audit IDs to ICS text on stdin.
 * Output: counts only. Server error messages can contain private source values.
 */
declare(strict_types=1);

if ($argc !== 2 || !is_file($argv[1])) {
    fwrite(STDERR, "Supply the Composer autoloader for sabre/vobject.\n");
    exit(2);
}
require $argv[1];
$requests = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
$results = [];
foreach ($requests as $id => $data) {
    $calendar = null;
    $errors = 0;
    $warnings = 0;
    $exception = false;
    try {
        $calendar = Sabre\VObject\Reader::read($data);
        if (!$calendar instanceof Sabre\VObject\Component\VCalendar) {
            throw new RuntimeException('Expected a calendar.');
        }
        foreach ($calendar->validate(Sabre\VObject\Node::PROFILE_CALDAV) as $message) {
            if ($message['level'] >= 3) {
                ++$errors;
            } elseif ($message['level'] === 2) {
                ++$warnings;
            }
        }
    } catch (Throwable $error) {
        $exception = true;
    } finally {
        if ($calendar !== null && method_exists($calendar, 'destroy')) {
            $calendar->destroy();
        }
    }
    $results[$id] = ['errors' => $errors, 'warnings' => $warnings, 'exception' => $exception];
}
echo json_encode($results, JSON_THROW_ON_ERROR), "\n";
