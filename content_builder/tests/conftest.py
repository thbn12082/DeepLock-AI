from __future__ import annotations

from pathlib import Path

import pytest

from deeplock_content.chunking import build_corpus
from deeplock_content.contracts import (
    PackContract,
    PackExpectedCounts,
    identity_graph_sha256,
    illustration_identities_sha256,
    outline_assignment_sha256,
)
from deeplock_content.extractors import extract_inputs
from deeplock_content.models import (
    AtomPlan,
    Course,
    FormulaBlock,
    FormulaSymbol,
    FullLesson,
    KnowledgeAtom,
    LearningPair,
    Lesson,
    MindMap,
    MindMapEdge,
    MindMapNode,
    ModuleOutline,
    PackData,
    Question,
    QuizOption,
    SourceExcerpt,
    TinyExample,
)
from deeplock_content.util import REPO_ROOT, sha256_bytes, sha256_json


@pytest.fixture()
def corpus():
    return build_corpus(extract_inputs(REPO_ROOT / "fixtures" / "gradient_descent.md"))


@pytest.fixture()
def valid_pack(corpus):
    ref = corpus.chunks[0].source_ref
    options = [
        QuizOption(id="A", text="Cộng gradient", rationale="Sai hướng", misconception_tag="SIGN"),
        QuizOption(id="B", text="Trừ learning rate nhân gradient", rationale="Đúng", misconception_tag=None),
        QuizOption(id="C", text="Không cập nhật", rationale="Bỏ qua gradient", misconception_tag="ZERO"),
        QuizOption(id="D", text="Nhân loss với batch", rationale="Nhầm đại lượng", misconception_tag="LOSS"),
    ]
    stems = [
        "Công thức nào đi ngược hướng gradient để giảm loss?",
        "Với w=2, gradient=3 và learning rate=0.1, trọng số mới bằng bao nhiêu theo quy tắc nào?",
        "Trong minimization, thao tác đúng với độ dịch chuyển learning-rate nhân gradient là gì?",
    ]
    atom_specs = [
        ("atom_gradient", "Gradient", []),
        ("atom_lr", "Learning rate", ["atom_gradient"]),
        ("atom_batch", "Batch gradient", ["atom_gradient"]),
    ]
    lessons = []
    questions = []
    pairs = []
    atoms = []
    plans = []
    for order_index, (atom_id, title, prerequisites) in enumerate(atom_specs):
        pair_id = f"pair_{atom_id}"
        lesson = Lesson(
            lesson_id=f"lesson_{atom_id}", pair_id=pair_id, atom_id=atom_id,
            title=title, learning_objective=f"Giải thích {title}.",
            grounding_type="SOURCE_GROUNDED", prerequisite_ids=prerequisites,
            hook="Đi xuống dốc.", intuition="Ta đi ngược gradient để giảm loss.",
            tiny_example=TinyExample(input="w=2", steps=["2-0.1×3"], output="1.7"),
            process_steps=["Đọc gradient", "Cập nhật"],
            formula_blocks=[FormulaBlock(
                latex="w'=w-eta*g", plain_text="Cập nhật",
                symbols=[FormulaSymbol(symbol="g", meaning="gradient")],
            )],
            common_mistake="Nhầm gradient với loss.", takeaways=["Đi ngược gradient."],
            recall_prompt="Vì sao có dấu trừ?", estimated_seconds=60,
            source_ref_ids=[ref.source_ref_id], illustration_source_ref_ids=[],
        )
        atom_questions = [
            Question(
                question_id=f"q_{atom_id}_{index}", pair_id=pair_id, atom_id=atom_id,
                stem=f"{title}: {stems[index - 1]}", options=options,
                correct_option_id="B", explanation="Ta đi ngược gradient.", difficulty=2,
                bloom_level="UNDERSTAND" if index < 3 else "APPLY",
                grounding_type="SOURCE_GROUNDED", source_ref_ids=[ref.source_ref_id],
            )
            for index in range(1, 4)
        ]
        pair_base = {
            "pair_id": pair_id, "atom_id": atom_id, "micro_lesson_id": lesson.lesson_id,
            "question_ids": [item.question_id for item in atom_questions], "revision": 1,
        }
        lessons.append(lesson)
        questions.extend(atom_questions)
        pairs.append(LearningPair(
            **pair_base, content_hash=sha256_json(pair_base), ai_review_status="PENDING",
        ))
        atoms.append(KnowledgeAtom(
            atom_id=atom_id, module_id="module_gradient", order_index=order_index,
            title=title, grounding_type="SOURCE_GROUNDED",
            prerequisite_ids=prerequisites, estimated_seconds=60,
        ))
        plans.append(AtomPlan(
            atom_id=atom_id, order_index=order_index, title=title,
            objective=f"Hiểu {title}", prerequisite_ids=prerequisites,
            source_ref_ids=[ref.source_ref_id],
        ))
    excerpt = SourceExcerpt(
        source_excerpt_id="excerpt_gradient", source_ref_id=ref.source_ref_id,
        document_id=ref.document_id, chunk_id=ref.chunk_id, document_label="fixture",
        heading_path=corpus.chunks[0].heading_path, page_start=1, page_end=1,
        context_text=corpus.chunks[0].normalized_text, highlight_start=0,
        highlight_end=len(corpus.chunks[0].normalized_text),
        excerpt_hash=sha256_bytes(corpus.chunks[0].normalized_text.encode()),
    )
    module = ModuleOutline(
        module_id="module_gradient", order_index=0, title="Optimization", objective="Hiểu gradient",
        atom_plans=plans,
    )
    course = Course(course_id="course_fixture", title="Gradient Descent", description="Fixture", language="vi")
    nodes = [
        MindMapNode(id=course.course_id, label=course.title, type="COURSE", atom_id=None),
        MindMapNode(id=module.module_id, label=module.title, type="MODULE", atom_id=None),
        *[
            MindMapNode(id=atom.atom_id, label=atom.title, type="ATOM", atom_id=atom.atom_id)
            for atom in atoms
        ],
    ]
    edges = [
        MindMapEdge(**{"from": course.course_id, "to": module.module_id, "type": "CONTAINS"}),
        *[
            MindMapEdge(**{"from": module.module_id, "to": atom.atom_id, "type": "CONTAINS"})
            for atom in atoms
        ],
        *[
            MindMapEdge(**{"from": prerequisite, "to": atom.atom_id, "type": "PREREQUISITE"})
            for atom in atoms for prerequisite in atom.prerequisite_ids
        ],
    ]
    return PackData(
        course=course,
        modules=[module],
        full_lessons=[FullLesson(
            full_lesson_id="full_gradient", module_id=module.module_id, order_index=0,
            title=module.title, objective=module.objective,
            atom_ids=[item.atom_id for item in atoms], estimated_minutes=5,
        )],
        atoms=atoms, lessons=lessons, learning_pairs=pairs, questions=questions,
        mindmaps=[MindMap(
            mind_map_id="map_gradient", root_node_id=course.course_id,
            nodes=nodes, edges=edges,
        )],
        mini_labs=[], illustrations=[], source_refs=[ref], source_excerpts=[excerpt], source_hash=corpus.source_hash,
        generator_model_snapshot="cx/gpt-5.5", generator_prompt_version="course-v1",
    )


@pytest.fixture()
def curriculum_pack(valid_pack):
    """A contract-compatible clone with each source assigned to one atom."""

    pack = valid_pack.model_copy(deep=True)
    original_ref = pack.source_refs[0]
    original_excerpt = pack.source_excerpts[0]
    refs = []
    excerpts = []
    for index, plan in enumerate(pack.modules[0].atom_plans, start=1):
        source_ref_id = f"src_contract_{index}"
        chunk_id = f"chunk_contract_{index}"
        ref = original_ref.model_copy(update={
            "source_ref_id": source_ref_id,
            "chunk_id": chunk_id,
        })
        excerpt = original_excerpt.model_copy(update={
            "source_excerpt_id": f"excerpt_contract_{index}",
            "source_ref_id": source_ref_id,
            "chunk_id": chunk_id,
        })
        plan.source_ref_ids = [source_ref_id]
        lesson = next(item for item in pack.lessons if item.atom_id == plan.atom_id)
        lesson.source_ref_ids = [source_ref_id]
        for question in pack.questions:
            if question.atom_id == plan.atom_id:
                question.source_ref_ids = [source_ref_id]
        refs.append(ref)
        excerpts.append(excerpt)
    pack.source_refs = refs
    pack.source_excerpts = excerpts
    return pack


@pytest.fixture()
def curriculum_contract(curriculum_pack):
    pack = curriculum_pack
    return PackContract(
        curriculum_sha256=sha256_json({"fixture": "curriculum"}),
        course_id=pack.course.course_id,
        course_sha256=sha256_json(pack.course.model_dump(mode="json")),
        source_hash=pack.source_hash,
        expected_counts=PackExpectedCounts(
            modules=len(pack.modules),
            full_lessons=len(pack.full_lessons),
            atoms=len(pack.atoms),
            lessons=len(pack.lessons),
            pairs=len(pack.learning_pairs),
            questions=len(pack.questions),
            source_refs=len(pack.source_refs),
            source_excerpts=len(pack.source_excerpts),
            illustrations=len(pack.illustrations),
            mindmaps=len(pack.mindmaps),
        ),
        questions_per_atom=3,
        ordered_module_ids=[item.module_id for item in pack.modules],
        ordered_atom_ids=[item.atom_id for item in pack.atoms],
        ordered_source_ref_ids_sha256=sha256_json([
            item.source_ref_id for item in pack.source_refs
        ]),
        ordered_source_refs_sha256=sha256_json([
            item.model_dump(mode="json") for item in pack.source_refs
        ]),
        ordered_source_excerpts_sha256=sha256_json([
            item.model_dump(mode="json") for item in pack.source_excerpts
        ]),
        ordered_module_source_ref_ids_sha256=[
            sha256_json([
                source_ref_id
                for plan in module.atom_plans
                for source_ref_id in plan.source_ref_ids
            ])
            for module in pack.modules
        ],
        identity_graph_sha256=identity_graph_sha256(pack),
        illustration_identities_sha256=illustration_identities_sha256(pack),
        outline_assignment_sha256=outline_assignment_sha256(pack),
    )
