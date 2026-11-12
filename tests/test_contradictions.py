from app.services.contradiction_rules import analyze_pair


def _kinds(claim: str, source: str) -> list[str]:
    findings, _ = analyze_pair(claim, source)
    return [f.kind for f in findings]


def test_increase_against_decrease_is_contradiction() -> None:
    assert "increase_decrease" in _kinds("El PIB creció en 2022.", "El PIB disminuyó en 2022.")


def test_negated_verb_does_not_count_as_opposite_direction() -> None:
    assert _kinds("El PIB creció en 2022.", "El PIB no creció en 2022.") == []


def test_true_against_false_is_contradiction() -> None:
    assert "true_false" in _kinds("Es verdadero que el tratado entró en vigor.", "Es falso que el tratado entró en vigor.")


def test_existence_against_absence_is_contradiction() -> None:
    claim = "Existe una ley de protección de datos en Colombia."
    source = "No existe una ley de protección de datos en Colombia."
    assert "existence_absence" in _kinds(claim, source)


def test_greater_against_lesser_is_contradiction() -> None:
    assert "greater_lesser" in _kinds("La deuda supera el 50% del PIB.", "La deuda es menor al 30% del PIB.")


def test_positive_against_negative_is_contradiction() -> None:
    assert "positive_negative" in _kinds("El resultado fue positivo.", "El resultado fue negativo.")


def test_same_percentage_context_different_value_is_percentage_contradiction() -> None:
    claim = "La tasa de desempleo en Colombia fue 10% en 2022."
    source = "La tasa de desempleo en Colombia fue 12% en 2022."
    assert _kinds(claim, source) == ["percentage"]


def test_same_event_different_year_is_date_contradiction() -> None:
    assert _kinds("Petro asumió la presidencia en 2022.", "Petro asumió la presidencia en 2018.") == ["date"]


def test_different_regions_are_discrepancy_not_contradiction() -> None:
    claim = "El desempleo en Colombia fue 10% en 2022."
    source = "El desempleo en Perú fue 12% en 2022."
    findings, notes = analyze_pair(claim, source)
    assert findings == []
    assert "región distinta" in notes


def test_different_periods_are_discrepancy_not_contradiction() -> None:
    findings, notes = analyze_pair("La inflación fue 5% en 2022.", "La inflación fue 3% en 2021.")
    assert findings == []
    assert "período distinto" in notes


def test_numbers_without_period_or_region_are_insufficient_not_contradiction() -> None:
    findings, notes = analyze_pair("La inflación fue 5%.", "La inflación fue 3%.")
    assert findings == []
    assert "período no determinado" in notes


def test_different_units_are_not_compared() -> None:
    findings, notes = analyze_pair("La población fue 5 millones en 2022.", "La población fue 5% en 2022.")
    assert findings == []
    assert "unidades distintas" in notes


def test_identical_statements_have_no_contradiction() -> None:
    assert _kinds("El PIB creció en 2022.", "El PIB creció en 2022.") == []
