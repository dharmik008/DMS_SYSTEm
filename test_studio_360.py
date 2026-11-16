"""
test_studio_360.py — Caryanams Studio 360° spin view.

Covers frame extraction from walkaround videos, the video upload endpoint, the
/studio/api/car-360 frame feed the viewer reads, and the Studio page markup.

Run from the project root:

    pytest test_studio_360.py
"""
import io
import re
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

import utils.sms
from app import create_app
from background.routes import StudioImage
from background.spin360 import (
    MAX_FRAME_WIDTH, SpinVideoError, extract_spin_frames, spaced_indices,
)
from models import User, db

SAMPLE_DEALER_EMAIL = 'dealer@caryanams.com'  # created by models.seed_demo_data()
SAMPLE_DEALER_PHONE = '9876543210'
PROJECT_ROOT = Path(__file__).resolve().parent
STUDIO_OUTPUT_DIRS = (PROJECT_ROOT / 'static' / 'images' / 'uploads',
                      PROJECT_ROOT / 'static' / 'processed')


def write_video(path: Path, frame_count: int, size=(64, 48)) -> Path:
    """Write an MJPG video whose frame i is a flat grey of brightness 3*i."""
    width, height = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 30, (width, height))
    for i in range(frame_count):
        writer.write(np.full((height, width, 3), (3 * i) % 256, np.uint8))
    writer.release()
    return path


def mean_brightness(path: Path) -> float:
    return float(cv2.imread(str(path)).mean())


# ── Frame extraction ──────────────────────────────────────────────────────────

def test_spaced_indices_spread_evenly_and_skip_the_closing_frame():
    assert spaced_indices(100, 4) == [0, 25, 50, 75]
    assert spaced_indices(36, 36) == list(range(36))


def test_spaced_indices_reject_more_frames_than_the_video_has():
    with pytest.raises(ValueError):
        spaced_indices(10, 24)


def test_extract_spin_frames_returns_evenly_spaced_frames_in_order(tmp_path):
    video = write_video(tmp_path / 'walk.avi', frame_count=72)

    frames = extract_spin_frames(video, tmp_path, 'spin', count=24)

    assert [p.name for p in frames] == [f'spin_{i:03d}.png' for i in range(24)]
    brightness = [mean_brightness(p) for p in frames]
    assert brightness == sorted(brightness)
    # Every third source frame, so brightness steps by about 9.
    assert brightness[1] - brightness[0] == pytest.approx(9, abs=2)


def test_extract_spin_frames_downscales_wide_video(tmp_path):
    video = write_video(tmp_path / 'wide.avi', frame_count=24, size=(2400, 120))

    frames = extract_spin_frames(video, tmp_path, 'spin', count=24)

    height, width = cv2.imread(str(frames[0])).shape[:2]
    assert width == MAX_FRAME_WIDTH
    assert height == 96


def test_too_short_video_raises_and_leaves_no_frames(tmp_path):
    video = write_video(tmp_path / 'short.avi', frame_count=10)
    out_dir = tmp_path / 'out'
    out_dir.mkdir()

    with pytest.raises(SpinVideoError, match='only 10 frames'):
        extract_spin_frames(video, out_dir, 'spin', count=24)
    assert not any(out_dir.iterdir())


def test_unreadable_file_raises(tmp_path):
    fake = tmp_path / 'fake.mp4'
    fake.write_bytes(b'definitely not a video')

    with pytest.raises(SpinVideoError):
        extract_spin_frames(fake, tmp_path, 'spin', count=24)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def remove_new_studio_files():
    """Studio writes into the project's static folders; delete what a test added."""
    before = {p for folder in STUDIO_OUTPUT_DIRS if folder.exists() for p in folder.iterdir()}
    yield
    for folder in STUDIO_OUTPUT_DIRS:
        if folder.exists():
            for path in set(folder.iterdir()) - before:
                path.unlink()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "studio_360.db"}')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.delenv('DEMO_LOGIN_ENABLED', raising=False)
    app = create_app()
    app.config['TESTING'] = True
    return app


@pytest.fixture
def dealer_on_plan(app, monkeypatch):
    """Returns sign_in(plan): a test client for the sample dealer on a paid `plan`."""
    sent_codes = []

    def capture_sms(phone, code):
        sent_codes.append(code)
        return True, None

    monkeypatch.setattr(utils.sms, 'send_otp_sms', capture_sms)

    def sign_in(plan='growth'):
        with app.app_context():
            assert User.query.filter_by(email=SAMPLE_DEALER_EMAIL).one()
        client = app.test_client()
        client.post('/auth/login',
                    data={'action': 'send', 'phone': SAMPLE_DEALER_PHONE, 'policy_agreed': 'on'})
        client.post('/auth/login', data={'action': 'verify', 'code': sent_codes[-1]})
        response = client.post('/dealer/subscription/activate', json={'plan': plan, 'method': 'demo'})
        assert response.get_json()['success'], response.get_json()
        return client

    return sign_in


@pytest.fixture
def studio(dealer_on_plan):
    return dealer_on_plan('growth')


def upload_video(client, video: Path, frame_count=24, filename=None):
    return client.post('/studio/api/upload-360-video', data={
        'video': (io.BytesIO(video.read_bytes()), filename or video.name),
        'frame_count': str(frame_count),
    }, content_type='multipart/form-data')


def png_bytes() -> io.BytesIO:
    buffer = io.BytesIO()
    Image.new('RGB', (40, 30), 'navy').save(buffer, 'PNG')
    buffer.seek(0)
    return buffer


def test_studio_page_includes_the_360_view(studio):
    html = studio.get('/studio/').get_data(as_text=True)

    assert 'id="spinStage"' in html
    assert 'data-studio-mode="spin"' in html
    assert '<option value="36" selected>' in html
    # Video upload is reachable from the canvas header, the empty 360° view and the panel.
    assert len(re.findall(r'<button[^>]*\bdata-spin-video-upload\b', html)) == 3
    assert 'id="spinVideoInput"' in html
    assert 'js/studio_360.js' in html
    # The existing preview canvas is still there.
    assert 'id="previewGrid"' in html and 'id="dropZone"' in html


def test_video_upload_builds_one_ordered_spin_set(studio, tmp_path):
    video = write_video(tmp_path / 'Swift walkaround.avi', frame_count=48)

    response = upload_video(studio, video, frame_count=24)

    assert response.status_code == 200
    frames = response.get_json()
    assert len(frames) == 24
    assert {f['view_angle'] for f in frames} == {'spin'}
    assert len({f['session_group'] for f in frames}) == 1
    assert frames[0]['car_name'] == 'Swift walkaround'
    # Saved through the normal upload pipeline, like photos.
    assert all(f['filename'] == f"car_{f['id']}.jpg" for f in frames)

    spin = studio.get(f"/studio/api/car-360/{frames[5]['id']}").get_json()
    assert [f['id'] for f in spin['frames']] == [f['id'] for f in frames]
    assert spin['is_multi_frame'] is True
    for frame in spin['frames'][:3]:
        assert studio.get(frame['url']).status_code == 200


def test_360_view_serves_frames_after_background_removal(studio, app):
    uploaded = studio.post('/studio/api/upload', data={
        'files': [(png_bytes(), 'front.png'), (png_bytes(), 'back.png')],
    }, content_type='multipart/form-data').get_json()

    # Studio writes No-BG cut-outs to static/processed; simulate one finished removal.
    with app.app_context():
        car = db.session.get(StudioImage, uploaded[0]['id'])
        nobg_path = Path(app.static_folder) / 'processed' / f'nobg_{car.id}.png'
        nobg_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGBA', (40, 30)).save(nobg_path)
        car.nobg_path = str(nobg_path)
        db.session.commit()

    spin = studio.get(f"/studio/api/car-360/{uploaded[0]['id']}").get_json()

    urls = [f['url'] for f in spin['frames']]
    assert urls[0] == f"/static/processed/nobg_{uploaded[0]['id']}.png"
    assert urls[1].startswith('/static/images/uploads/')
    assert all(studio.get(url).status_code == 200 for url in urls)


def test_360_view_of_unknown_image_is_404(studio):
    response = studio.get('/studio/api/car-360/does-not-exist')

    assert response.status_code == 404
    assert response.get_json()['error'] == 'Image not found'


@pytest.mark.parametrize('filename, frame_count, message', [
    ('walk.gif', 24, 'Unsupported video type'),
    ('walk.avi', 30, 'Frame count must be one of'),
    ('walk.avi', 'many', 'Frame count must be one of'),
])
def test_video_upload_rejects_bad_input(studio, tmp_path, filename, frame_count, message):
    video = write_video(tmp_path / 'walk.avi', frame_count=48)

    response = upload_video(studio, video, frame_count=frame_count, filename=filename)

    assert response.status_code == 400
    assert message in response.get_json()['error']


def test_video_upload_without_a_file_is_rejected(studio):
    response = studio.post('/studio/api/upload-360-video', data={'frame_count': '24'})

    assert response.status_code == 400
    assert response.get_json()['error'] == 'No video received'


def test_too_short_video_is_rejected_without_creating_images(studio, app, tmp_path):
    video = write_video(tmp_path / 'short.avi', frame_count=12)

    response = upload_video(studio, video, frame_count=24)

    assert response.status_code == 422
    assert 'only 12 frames' in response.get_json()['error']
    with app.app_context():
        assert StudioImage.query.count() == 0


def test_starter_plan_cannot_upload_a_360_video(dealer_on_plan, tmp_path):
    client = dealer_on_plan('starter')
    video = write_video(tmp_path / 'walk.avi', frame_count=48)

    response = upload_video(client, video)

    assert response.status_code == 403
    assert response.get_json()['error'] == 'FEATURE_NOT_AVAILABLE'
