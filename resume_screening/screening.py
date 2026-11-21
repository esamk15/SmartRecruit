
import re
import pymupdf
from docx import Document
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity


# Load the semantic model once and reuse it
model = SentenceTransformer("all-MiniLM-L6-v2")


# Technical skills SmartRecruit can recognise
SKILLS = [
    "python", "java", "c++", "javascript", "typescript",
    "html", "css", "react", "angular", "node.js",
    "flask", "django", "fastapi",
    "sql", "mysql", "postgresql", "mongodb",
    "machine learning", "deep learning", "artificial intelligence",
    "data science", "data analysis", "pandas", "numpy",
    "scikit-learn", "tensorflow", "pytorch",
    "excel", "power bi", "tableau",
    "aws", "azure", "google cloud",
    "docker", "kubernetes", "git", "linux",
    "rest api", "api", "communication", "problem solving"
]


# Recognised educational qualifications
EDUCATION_TERMS = [
    "b.tech", "btech", "b.e.", "bachelor of engineering",
    "bachelor of technology", "bachelor's degree",
    "b.sc", "bca", "mca", "m.tech", "mtech",
    "master's degree", "mba", "phd", "diploma"
]


def extract_pdf_text(file_path):
    text = ""

    with pymupdf.open(file_path) as pdf:
        for page in pdf:
            text += page.get_text() + "\n"

    return text


def extract_docx_text(file_path):
    document = Document(file_path)

    paragraphs = [p.text for p in document.paragraphs]

    # Also extract text from tables
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.append(cell.text)

    return "\n".join(paragraphs)


def extract_resume_text(file_path):
    if file_path.lower().endswith(".pdf"):
        return extract_pdf_text(file_path)

    elif file_path.lower().endswith(".docx"):
        return extract_docx_text(file_path)

    raise ValueError("Only PDF and DOCX files are supported.")


def find_skills(text):
    text = text.lower()
    found = []

    for skill in SKILLS:
        pattern = r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])"

        if re.search(pattern, text):
            found.append(skill)

    return found


def semantic_score(resume_text, job_description):
    embeddings = model.encode(
        [job_description, resume_text]
    )

    similarity = cosine_similarity(
        [embeddings[0]],
        [embeddings[1]]
    )[0][0]

    # Convert similarity into a percentage
    return max(0, min(100, float(similarity) * 100))


def skills_score(resume_text, job_description):
    required_skills = find_skills(job_description)
    candidate_skills = find_skills(resume_text)

    if not required_skills:
        return None, [], []

    matched = [
        skill for skill in required_skills
        if skill in candidate_skills
    ]

    missing = [
        skill for skill in required_skills
        if skill not in candidate_skills
    ]

    score = (len(matched) / len(required_skills)) * 100

    return score, matched, missing


def experience_score(resume_text, job_description):
    # Look for required experience such as "3 years of experience"
    requirement = re.search(
        r"(\d+)\+?\s*(?:years|year|yrs|yr)\s+(?:of\s+)?experience",
        job_description.lower()
    )

    if not requirement:
        return None

    required_years = int(requirement.group(1))

    # Look for years of experience mentioned in the resume
    resume_years = re.findall(
        r"(\d+)\+?\s*(?:years|year|yrs|yr)\s+(?:of\s+)?experience",
        resume_text.lower()
    )

    if not resume_years:
        return 0

    candidate_years = max(int(year) for year in resume_years)

    return min(100, (candidate_years / required_years) * 100)


def education_score(resume_text, job_description):
    job_lower = job_description.lower()
    resume_lower = resume_text.lower()

    required = [
        term for term in EDUCATION_TERMS
        if term in job_lower
    ]

    if not required:
        return None

    matched = any(term in resume_lower for term in required)

    return 100 if matched else 0


def calculate_match(resume_text, job_description):
    if not resume_text.strip():
        raise ValueError(
            "No readable text found in the resume. "
            "Please upload a text-based PDF or DOCX file."
        )

    if not job_description.strip():
        raise ValueError("Please enter a job description.")

    # Calculate individual scores
    semantic = semantic_score(resume_text, job_description)

    skill, matched_skills, missing_skills = skills_score(
        resume_text, job_description
    )

    experience = experience_score(
        resume_text, job_description
    )

    education = education_score(
        resume_text, job_description
    )

    # Only include categories that apply to the job description
    weights = {
        "semantic": 40,
        "skills": 35,
        "experience": 15,
        "education": 10
    }

    scores = {
        "semantic": semantic,
        "skills": skill,
        "experience": experience,
        "education": education
    }

    active_weight = sum(
        weights[key]
        for key in scores
        if scores[key] is not None
    )

    final_score = sum(
        scores[key] * weights[key]
        for key in scores
        if scores[key] is not None
    ) / active_weight

    return {
        "match_score": round(final_score, 2),
        "score_breakdown": {
            key: round(value, 2) if value is not None else None
            for key, value in scores.items()
        },
        "matched_skills": matched_skills,
        "missing_skills": missing_skills
    }


def screen_resume(file_path, job_description):
    resume_text = extract_resume_text(file_path)

    result = calculate_match(
        resume_text,
        job_description
    )

    result["resume_text"] = resume_text

    return result
