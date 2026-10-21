"""core.skills 单元测试：内置 Skill 手册结构、frontmatter、清单与读取。

Skill 手册是 AI 行为的参考文档；加载/校验逻辑放在纯标准库模块里，便于本地测试，
工具层（core/tools/ 的 phone_skill）只做薄封装。
"""

from __future__ import annotations

import pytest

from core.skills import (
    INJECTION_NOTICE,
    MAX_SKILL_BYTES,
    SKILL_FILENAME,
    SkillError,
    default_skills_dir,
    list_skills,
    parse_frontmatter,
    render_catalog,
    render_skill,
    validate_skill_name,
)

EXPECTED_SKILLS = {
    "phone-autonomy",
    "phone-housekeeping",
    "phone-interactions",
    "phone-verification",
    "phone-media",
}


def test_builtin_skill_directories_exist():
    base = default_skills_dir()
    assert base.is_dir()
    assert base.name == "skills"
    dirs = {child.name for child in base.iterdir() if child.is_dir()}
    assert EXPECTED_SKILLS <= dirs
    for name in EXPECTED_SKILLS:
        path = base / name / SKILL_FILENAME
        assert path.is_file(), path
        assert path.stat().st_size > 0


def test_builtin_frontmatter_is_valid():
    infos = list_skills()
    assert {info.name for info in infos} == EXPECTED_SKILLS
    for info in infos:
        assert info.description
        text = info.path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        assert meta.get("name") == info.name, info.path
        assert meta.get("description"), info.path
        assert body.strip(), info.path


def test_parse_frontmatter_handles_plain_markdown():
    meta, body = parse_frontmatter("# 标题\n正文内容")
    assert meta == {}
    assert body == "# 标题\n正文内容"


def test_parse_frontmatter_reads_quoted_values():
    text = '---\nname: "demo"\ndescription: \'示例手册\'\n---\n正文'
    meta, body = parse_frontmatter(text)
    assert meta == {"name": "demo", "description": "示例手册"}
    assert body == "正文"


def test_list_skills_is_sorted():
    infos = list_skills()
    assert [info.name for info in infos] == sorted(
        info.name for info in infos
    )


def test_render_catalog_lists_every_skill():
    catalog = render_catalog()
    for name in EXPECTED_SKILLS:
        assert name in catalog, catalog
    assert "phone_skill" in catalog
    assert "被动参考" in catalog


def test_render_autonomy_skill():
    text = render_skill("phone-autonomy")
    assert INJECTION_NOTICE in text
    assert "手机自主浏览手册" in text
    assert "phone_browse" in text
    assert "不是必经流程" in text
    assert len(text.encode("utf-8")) <= MAX_SKILL_BYTES + 4096


def test_render_housekeeping_skill():
    text = render_skill("phone-housekeeping")
    assert INJECTION_NOTICE in text
    assert "phone_clean_background" in text
    assert "background_keep_packages" in text
    assert "am force-stop" in text
    assert "前台" in text


def test_render_interactions_skill():
    text = render_skill("phone-interactions")
    assert INJECTION_NOTICE in text
    assert "phone_interaction_gate" in text
    assert "phone_interaction_record" in text
    assert "phone_screen" in text
    assert "不要反复重试" in text
    assert "只点一次" in text
    assert len(text.encode("utf-8")) <= MAX_SKILL_BYTES + 4096


def test_render_phone_verification_skill():
    """人机验证手册：frontmatter 完整，正文覆盖 bypass 四种动作。"""
    info = next(
        item for item in list_skills() if item.name == "phone-verification"
    )
    assert info.description
    text = info.path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    assert meta.get("name") == "phone-verification"
    assert meta.get("description")
    assert body.strip()
    for keyword in (
        "phone_bypass_verify",
        "capture",
        "drag",
        "tap_points",
        "slide",
    ):
        assert keyword in body, keyword
    rendered = render_skill("phone-verification")
    assert INJECTION_NOTICE in rendered
    assert len(rendered.encode("utf-8")) <= MAX_SKILL_BYTES + 4096


def test_unknown_skill_rejected_with_available_names():
    with pytest.raises(SkillError) as excinfo:
        render_skill("not-exist")
    message = str(excinfo.value)
    assert "未知 skill" in message
    for name in EXPECTED_SKILLS:
        assert name in message


@pytest.mark.parametrize(
    "bad_name",
    ["", "  ", "../phone-autonomy", "a/b", "name with space", "x;y", "a\nb"],
)
def test_invalid_skill_name_rejected(bad_name):
    with pytest.raises(SkillError):
        validate_skill_name(bad_name)


def test_oversized_skill_is_truncated(tmp_path):
    skill_dir = tmp_path / "big-skill"
    skill_dir.mkdir()
    (skill_dir / SKILL_FILENAME).write_text(
        "---\nname: big-skill\ndescription: 大手册\n---\n" + "内容" * 4000,
        encoding="utf-8",
    )
    text = render_skill("big-skill", root=tmp_path)
    assert INJECTION_NOTICE in text
    assert "已截断" in text
    assert len(text.encode("utf-8")) > MAX_SKILL_BYTES


def test_missing_skills_dir_raises(tmp_path):
    with pytest.raises(SkillError):
        list_skills(tmp_path / "not-exists")
