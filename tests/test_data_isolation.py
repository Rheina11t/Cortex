import pytest
from unittest.mock import MagicMock, patch
import sys
import os

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src import brain
from src import family_invites

# Mock family IDs
FAMILY_A = "family_aaa111"
FAMILY_B = "family_bbb222"

@pytest.fixture
def mock_supabase():
    with patch("src.brain._supabase") as mock_db:
        # Mock the chainable Supabase client
        mock_query = MagicMock()
        mock_db.table.return_value = mock_query
        mock_query.select.return_value = mock_query
        mock_query.contains.return_value = mock_query
        mock_query.eq.return_value = mock_query
        mock_query.order.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.execute.return_value = MagicMock(data=[])
        
        yield mock_db

def test_memory_query_isolation(mock_supabase):
    """Verify that memory queries always include the family_id filter."""
    mock_query = mock_supabase.table.return_value
    
    # Call a memory listing function
    brain.list_recent_memories(limit=10, family_id=FAMILY_A)
    
    # Verify that .contains("metadata", {"family_id": FAMILY_A}) was called
    mock_query.contains.assert_called_with("metadata", {"family_id": FAMILY_A})
    
    # Verify it doesn't leak to another family
    brain.list_recent_memories(limit=10, family_id=FAMILY_B)
    mock_query.contains.assert_called_with("metadata", {"family_id": FAMILY_B})

def test_invite_token_isolation(mock_supabase):
    """Verify that invite token lookups are scoped correctly."""
    mock_query = mock_supabase.table.return_value
    mock_query.execute.return_value.data = [{"family_id": FAMILY_A, "invite_token": "token123"}]
    
    with patch("src.family_invites._get_supabase", return_value=mock_supabase):
        invite = family_invites.get_invite("token123")
        assert invite["family_id"] == FAMILY_A
        # The query itself should be by token
        mock_query.eq.assert_called_with("invite_token", "token123")

@patch("src.emergency_pdf.brain._supabase")
def test_sos_pdf_isolation(mock_brain_db):
    """Verify that SOS PDF generation only pulls data for the specific family."""
    from src.emergency_pdf import generate_emergency_pdf
    
    mock_query = MagicMock()
    mock_brain_db.table.return_value = mock_query
    mock_query.select.return_value = mock_query
    mock_query.eq.return_value = mock_query
    mock_query.contains.return_value = mock_query
    mock_query.execute.return_value.data = []
    
    # Generate PDF for Family A
    try:
        generate_emergency_pdf(FAMILY_A)
    except Exception:
        pass # We only care about the calls made to the DB
    
    # Check that queries were filtered by FAMILY_A
    called_with_family = False
    for call in mock_query.eq.call_args_list:
        if call.args == ("family_id", FAMILY_A):
            called_with_family = True
    for call in mock_query.contains.call_args_list:
        if call.args == ("metadata", {"family_id": FAMILY_A}):
            called_with_family = True
            
    assert called_with_family, "Queries for SOS PDF must be filtered by family_id"
